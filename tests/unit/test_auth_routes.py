"""Roadmap M4.1a: the authentication routes through the real application, with a fake clock and a fake security log.

What is checked: every route under /api/v1 except the two that start a session needs one; the pairing and session flows;
the uniform answers; fail-closed readiness; nothing secret in any answer or event; and that the document and ask
endpoints behave exactly as before, with the connector off and on (ADR-007, decisions D-1 and D-15).
"""
import json
import re

import pytest
from fastapi.testclient import TestClient

from rag import config, health
from rag.api import create_app
from rag.auth.checks import SecurityChecks
from rag.auth.deps import AuthContext, AuthError, Principal
from rag.auth.events import CHECK_NAMES
from rag.auth.pairing import PairingBoard
from tests.auth_support import OWNER_ID, Clock, Events, FakePrincipals

MINUTE = 60
PUBLIC = {("POST", "/api/v1/session/pairing"), ("POST", "/api/v1/session/exchange"), ("GET", "/api/v1/connector")}


class Env:
    """One application with the connector on, and everything a test needs to move or inspect."""

    def __init__(self, repo):
        self.clock, self.events, self.shown = Clock(), Events(), []
        self.flags = {name: True for name in CHECK_NAMES}
        checks = {name: (lambda n=name: self.flags[n]) for name in CHECK_NAMES}
        checks["security_log_writable"] = lambda: self.flags["security_log_writable"] and self.events.check()
        checks = SecurityChecks(checks, self.events, self.clock)
        self.ctx = AuthContext(FakePrincipals(), self.events, checks=checks, clock=self.clock, dirs=[],
                               pairing=PairingBoard(self.clock, show=self.shown.append))
        self.app = create_app(repo, security=self.ctx)
        self.client = TestClient(self.app, base_url=f"http://127.0.0.1:{config.PORT}")

    def code(self):
        match = re.search(r"(\d{4}) (\d{4})", self.shown[-1])
        return match.expand(r"\1\2")

    def sign_in(self):
        self.clock.advance(config.PAIRING_REQUEST_GAP_S)
        assert self.client.post("/api/v1/session/pairing").status_code == 200
        answer = self.client.post("/api/v1/session/exchange", json={"code": self.code()})
        assert answer.status_code == 200, answer.text
        return answer.json()["token"]

    @staticmethod
    def bearer(token):
        return {"Authorization": f"Bearer {token}"}


def v1_routes(app):
    """(method, path) of every route under /api/v1, read from the application's own description of itself."""
    return [(method.upper(), path) for path, item in app.openapi()["paths"].items() if path.startswith("/api/v1")
            for method in item]


@pytest.fixture
def env(storage_dir, repo, qdrant, monkeypatch):
    monkeypatch.setattr(health, "check_qdrant", lambda: "ok")
    monkeypatch.setattr(health, "check_ollama", lambda: "ok")
    environment = Env(repo)
    with environment.client:  # runs the start-up of the application, as a real start does
        yield environment


# ---------- the connector is absent unless it is enabled ----------

def test_without_the_connector_there_are_no_new_routes_and_readiness_is_as_it_always_was(
        storage_dir, repo, qdrant, monkeypatch):
    monkeypatch.setattr(health, "check_qdrant", lambda: "ok")
    monkeypatch.setattr(health, "check_ollama", lambda: "ok")
    monkeypatch.setattr(config, "CONNECTOR_ENABLED", False)
    app = create_app(repo)
    assert v1_routes(app) == [("GET", "/api/v1/connector")]  # the only one: it says the connector is off
    with TestClient(app, base_url=f"http://127.0.0.1:{config.PORT}") as client:
        assert client.get("/api/v1/connector").json() == {"enabled": False}
        assert client.get("/api/v1/session").status_code == 404  # no session routes: the connector is off
        ready = client.get("/readyz")
        assert ready.status_code == 200 and ready.json() == {
            "status": "ready", "checks": {"postgres": "ok", "qdrant": "ok", "ollama": "ok"}}
        assert "connector" not in client.get("/api/status").json()["dependencies"]


def test_the_setting_turns_the_connector_on_and_off(monkeypatch):
    monkeypatch.setenv("RAG_CONNECTOR_ENABLED", "true")
    import importlib

    from rag import config as fresh
    importlib.reload(fresh)
    try:
        assert fresh.CONNECTOR_ENABLED is True
        monkeypatch.setenv("RAG_CONNECTOR_ENABLED", "yes")  # anything else than 1/true is off
        importlib.reload(fresh)
        assert fresh.CONNECTOR_ENABLED is False
    finally:
        monkeypatch.delenv("RAG_CONNECTOR_ENABLED", raising=False)
        importlib.reload(fresh)
    assert fresh.CONNECTOR_ENABLED is False  # the default: off


# ---------- who needs a session ----------

def test_every_route_under_api_v1_needs_a_session_except_the_two_that_start_one(env):
    routes = v1_routes(env.app)
    assert len(routes) >= 8 and PUBLIC <= set(routes)
    for method, path in routes:
        answer = env.client.request(method, path)
        if (method, path) in PUBLIC:
            assert answer.status_code != 401, (method, path)
        else:
            assert answer.status_code == 401, (method, path, answer.status_code)
            assert answer.json() == {"error": {"code": "unauthenticated"}}
            assert answer.headers["www-authenticate"] == "Bearer"


def test_a_wrong_token_and_other_places_for_a_token_do_not_count(env):
    token = env.sign_in()
    assert env.client.get("/api/v1/session", headers=Env.bearer("wrong")).status_code == 401
    assert env.client.get(f"/api/v1/session?token={token}").status_code == 401  # not in the query
    assert env.client.get("/api/v1/session", headers={"Cookie": f"token={token}"}).status_code == 401  # not a cookie
    assert env.client.get("/api/v1/session", headers={"Authorization": f"Basic {token}"}).status_code == 401
    assert env.client.get("/api/v1/session", headers={"Authorization": "Bearer "}).status_code == 401
    assert env.client.get("/api/v1/session", headers=Env.bearer(token)).status_code == 200


def test_a_session_is_for_the_local_owner_and_nobody_else_may_use_the_connector(env):
    with pytest.raises(AuthError) as error:
        env.ctx.authorize(Principal("someone", "user"), "anything")
    assert error.value.status == 403
    env.ctx.authorize(Principal(OWNER_ID, "local-owner"), "anything")  # the local owner may


def test_the_one_decision_point_is_in_the_path_of_every_authenticated_request(env):
    token = env.sign_in()

    class Stranger:
        def local_owner(self):
            return {"id": OWNER_ID, "kind": "user"}

    env.ctx.principals = Stranger()
    env.clock.advance(config.SECURITY_CHECK_TTL_S + 1)  # the cached owner is looked up again
    answer = env.client.get("/api/v1/session", headers=Env.bearer(token))
    assert answer.status_code == 403 and answer.json() == {"error": {"code": "forbidden"}}


def test_a_token_far_longer_than_a_real_one_is_malformed_without_being_looked_up(env):
    answer = env.client.get("/api/v1/session", headers=Env.bearer("x" * 5000))
    assert answer.status_code == 401 and ("session_rejected", "malformed") in env.events.names()


def test_a_console_that_cannot_be_written_to_is_unavailable_not_a_server_error(env):
    def broken(_text):
        raise OSError("no console")

    env.ctx.pairing._show = broken
    answer = env.client.post("/api/v1/session/pairing")
    assert answer.status_code == 503 and answer.json() == {"error": {"code": "connector_unavailable"}}
    assert ("pairing_requested", "unavailable") in env.events.names()


# ---------- pairing and the session ----------

def test_the_code_is_never_in_the_answer_only_in_the_console(env):
    answer = env.client.post("/api/v1/session/pairing")
    assert answer.status_code == 200 and answer.json() == {"requested": True, "ttl_s": config.PAIRING_TTL_S}
    assert env.code() not in answer.text and env.code() not in str(answer.headers)


def test_a_full_session_pairing_use_and_end(env):
    token = env.sign_in()
    state = env.client.get("/api/v1/session", headers=Env.bearer(token)).json()
    assert state["principal"] == {"id": OWNER_ID, "kind": "local-owner"}
    assert state["absolute_remaining_s"] == 12 * MINUTE and state["idle_remaining_s"] == 2 * MINUTE
    assert state["step_up_remaining_s"] == 0
    assert env.client.delete("/api/v1/session", headers=Env.bearer(token)).status_code == 204
    assert env.client.get("/api/v1/session", headers=Env.bearer(token)).status_code == 401


def test_the_exchange_returns_a_token_and_the_limits(env):
    env.client.post("/api/v1/session/pairing")
    answer = env.client.post("/api/v1/session/exchange", json={"code": env.code()}).json()
    assert set(answer) == {"token", "expires_in_s", "idle_timeout_s"}
    assert answer["expires_in_s"] == 12 * MINUTE and answer["idle_timeout_s"] == 2 * MINUTE


def test_a_wrong_expired_and_reused_code_are_the_same_answer(env):
    env.client.post("/api/v1/session/pairing")
    code = env.code()
    answers = []
    answers.append(env.client.post("/api/v1/session/exchange", json={"code": "00000000"}))  # wrong (uses it up)
    answers.append(env.client.post("/api/v1/session/exchange", json={"code": code}))  # reused / gone
    env.clock.advance(config.PAIRING_REQUEST_GAP_S)
    env.client.post("/api/v1/session/pairing")
    env.clock.advance(config.PAIRING_TTL_S)
    answers.append(env.client.post("/api/v1/session/exchange", json={"code": env.code()}))  # expired
    assert [a.status_code for a in answers] == [401, 401, 401]
    assert all(a.json() == {"error": {"code": "pairing_invalid"}} for a in answers)


def test_asking_for_codes_too_fast_is_answered_with_429_and_retry_after(env):
    assert env.client.post("/api/v1/session/pairing").status_code == 200
    answer = env.client.post("/api/v1/session/pairing")
    assert answer.status_code == 429 and int(answer.headers["retry-after"]) >= config.PAIRING_REQUEST_GAP_S
    assert answer.json() == {"error": {"code": "rate_limited"}} and len(env.shown) == 1


def test_guessing_codes_leads_to_429_and_a_lockout(env):
    env.client.post("/api/v1/session/pairing")
    statuses = [env.client.post("/api/v1/session/exchange", json={"code": "12345678"}).status_code
                for _ in range(config.PAIRING_ATTEMPTS + 1)]
    assert statuses == [401] * config.PAIRING_ATTEMPTS + [429]


def test_a_body_that_is_not_a_code_is_not_an_attempt(env):
    env.client.post("/api/v1/session/pairing")
    for body in ({}, {"code": ""}, {"code": "x" * 41}, {"nothing": 1}):
        assert env.client.post("/api/v1/session/exchange", json=body).status_code == 422
    assert env.client.post("/api/v1/session/exchange", json={"code": env.code()}).status_code == 200


def test_a_session_idles_out_and_the_answer_says_it_expired(env):
    token = env.sign_in()
    env.clock.advance(2 * MINUTE)
    answer = env.client.get("/api/v1/session", headers=Env.bearer(token))
    assert answer.status_code == 401 and answer.json() == {"error": {"code": "session_expired"}}
    assert ("session_revoked", "idle") in env.events.names()


def test_a_session_ends_at_twelve_minutes_however_busy(env):
    token = env.sign_in()
    for _ in range(11):  # used every minute, so it never idles; each use of rotate also replaces the token
        env.clock.advance(MINUTE)
        answer = env.client.post("/api/v1/session/rotate", headers=Env.bearer(token))
        assert answer.status_code == 200
        token = answer.json()["token"]
    env.clock.advance(MINUTE)
    answer = env.client.post("/api/v1/session/rotate", headers=Env.bearer(token))
    assert answer.status_code == 401 and answer.json() == {"error": {"code": "session_expired"}}
    assert ("session_revoked", "absolute") in env.events.names()


def test_looking_at_the_session_does_not_keep_it_alive(env):
    token = env.sign_in()
    env.clock.advance(MINUTE + 30)
    assert env.client.get("/api/v1/session", headers=Env.bearer(token)).status_code == 200
    env.clock.advance(MINUTE)
    assert env.client.get("/api/v1/session", headers=Env.bearer(token)).status_code == 401  # idle since the sign-in


def test_rotation_replaces_the_token_and_keeps_the_end_of_the_session(env):
    token = env.sign_in()
    env.clock.advance(MINUTE)
    new = env.client.post("/api/v1/session/rotate", headers=Env.bearer(token)).json()["token"]
    assert new != token and env.client.get("/api/v1/session", headers=Env.bearer(token)).status_code == 401
    state = env.client.get("/api/v1/session", headers=Env.bearer(new)).json()
    assert state["absolute_remaining_s"] == 11 * MINUTE


def test_ending_all_sessions(env):
    first = env.sign_in()
    second = env.sign_in()
    assert env.client.delete("/api/v1/sessions", headers=Env.bearer(first)).status_code == 204
    assert env.client.get("/api/v1/session", headers=Env.bearer(first)).status_code == 401
    assert env.client.get("/api/v1/session", headers=Env.bearer(second)).status_code == 401


# ---------- step-up ----------

def step_up(env, token):
    env.clock.advance(config.PAIRING_REQUEST_GAP_S)
    assert env.client.post("/api/v1/session/pairing", json={"purpose": "step-up"},
                           headers=Env.bearer(token)).status_code == 200
    return env.client.post("/api/v1/session/step-up", json={"code": env.code()}, headers=Env.bearer(token))


def test_a_step_up_needs_a_session_to_ask_for_its_code(env):
    assert env.client.post("/api/v1/session/pairing", json={"purpose": "step-up"}).status_code == 401


def test_a_step_up_confirms_replaces_the_token_and_counts_ten_minutes_at_most(env):
    token = env.sign_in()
    answer = step_up(env, token)
    assert answer.status_code == 200
    new = answer.json()["token"]
    assert new != token and env.client.get("/api/v1/session", headers=Env.bearer(token)).status_code == 401
    left = env.client.get("/api/v1/session", headers=Env.bearer(new)).json()
    assert 0 < left["step_up_remaining_s"] <= 10 * MINUTE
    assert left["step_up_remaining_s"] <= left["absolute_remaining_s"]


def test_a_step_up_never_outlives_the_session(env):
    token = env.sign_in()
    for _ in range(5):
        env.clock.advance(MINUTE)
        token = env.client.post("/api/v1/session/rotate", headers=Env.bearer(token)).json()["token"]
    answer = step_up(env, token)
    left = env.client.get("/api/v1/session", headers=Env.bearer(answer.json()["token"])).json()
    assert left["step_up_remaining_s"] <= left["absolute_remaining_s"] <= 7 * MINUTE


def test_a_login_code_does_not_confirm_a_step_up(env):
    token = env.sign_in()
    env.clock.advance(config.PAIRING_REQUEST_GAP_S)
    env.client.post("/api/v1/session/pairing")  # a login code
    answer = env.client.post("/api/v1/session/step-up", json={"code": env.code()}, headers=Env.bearer(token))
    assert answer.status_code == 401 and answer.json() == {"error": {"code": "pairing_invalid"}}


def test_a_route_that_needs_a_step_up_refuses_until_it_is_confirmed_and_after_it_has_run_out(env):
    from typing import Annotated

    from fastapi import Depends

    @env.app.get("/api/v1/needs-step-up")
    def protected(principal: Annotated[object, Depends(env.ctx.require_step_up)]):
        return {"ok": principal.kind}

    token = env.sign_in()
    answer = env.client.get("/api/v1/needs-step-up", headers=Env.bearer(token))
    assert answer.status_code == 403 and answer.json() == {"error": {"code": "step_up_required"}}
    token = step_up(env, token).json()["token"]
    assert env.client.get("/api/v1/needs-step-up", headers=Env.bearer(token)).json() == {"ok": "local-owner"}
    for _ in range(10):  # busy for ten minutes: the session lives, the step-up runs out
        env.clock.advance(MINUTE)
        token = env.client.post("/api/v1/session/rotate", headers=Env.bearer(token)).json()["token"]
    assert env.client.get("/api/v1/needs-step-up", headers=Env.bearer(token)).status_code == 403


# ---------- fail closed ----------

def test_when_a_security_condition_fails_nothing_new_starts_and_readiness_says_so(env):
    token = env.sign_in()
    env.flags["db_role_unprivileged"] = False
    env.clock.advance(config.SECURITY_CHECK_TTL_S)  # the result of the checks is reused until then
    assert env.client.post("/api/v1/session/pairing").status_code == 503
    assert env.client.post("/api/v1/session/exchange", json={"code": "12345678"}).status_code == 503
    ready = env.client.get("/readyz")
    assert ready.status_code == 503 and ready.json()["status"] == "not_ready"
    assert ready.json()["checks"]["connector"] == "unavailable"
    assert env.client.get("/api/status").json()["ready"] == "not_ready"
    refused = env.client.get("/api/v1/session", headers=Env.bearer(token))
    assert refused.status_code == 503 and refused.json() == {"error": {"code": "connector_unavailable"}}
    # an existing session can still read the status: it is how the reason is found
    status = env.client.get("/api/v1/connector/status", headers=Env.bearer(token)).json()
    assert status["ready"] is False and status["checks"]["db_role_unprivileged"] is False
    assert ("readiness_security_changed", "check_failed") in env.events.names()


def test_the_connector_becomes_available_again_when_the_condition_is_restored(env):
    env.flags["secrets_dir_private"] = False
    env.clock.advance(config.SECURITY_CHECK_TTL_S)
    assert env.client.get("/readyz").status_code == 503
    env.flags["secrets_dir_private"] = True
    env.clock.advance(config.SECURITY_CHECK_TTL_S)
    assert env.client.get("/readyz").status_code == 200
    assert ("readiness_security_changed", "ok") in env.events.names()


def test_a_session_is_not_granted_if_its_security_event_cannot_be_recorded(env):
    env.clock.advance(config.PAIRING_REQUEST_GAP_S)
    env.client.post("/api/v1/session/pairing")
    env.events.lost = True
    answer = env.client.post("/api/v1/session/exchange", json={"code": env.code()})
    assert answer.status_code == 503 and answer.json() == {"error": {"code": "security_log_unavailable"}}
    assert env.ctx.sessions.count() == 0  # nothing was left behind


def test_a_lost_event_makes_the_connector_unavailable_until_a_write_works_again(env):
    env.events.lost = True
    env.events.emit("session_created", "ok")  # the failed write marks the log unhealthy
    env.clock.advance(config.SECURITY_CHECK_TTL_S)
    assert env.client.post("/api/v1/session/pairing").status_code == 503
    env.events.lost = False
    env.events.emit("session_created", "ok")
    env.clock.advance(config.SECURITY_CHECK_TTL_S)
    assert env.client.post("/api/v1/session/pairing").status_code == 200


# ---------- the rest of the application is untouched ----------

def test_the_existing_endpoints_need_no_token_and_carry_no_new_headers_with_the_connector_on(env):
    for method, path in (("GET", "/api/documents"), ("GET", "/healthz"), ("GET", "/readyz"), ("GET", "/api/status"),
                         ("GET", "/")):
        answer = env.client.request(method, path)
        assert answer.status_code == 200, (path, answer.status_code)
        assert "referrer-policy" not in answer.headers and "cache-control" not in answer.headers, path
    assert env.client.get("/readyz").json()["checks"]["connector"] == "ok"
    assert env.client.get("/api/status").json()["dependencies"]["connector"] == "ok"


def test_the_new_routes_say_no_store_and_no_referrer_even_in_their_errors(env):
    for answer in (env.client.post("/api/v1/session/pairing"), env.client.get("/api/v1/session"),
                   env.client.post("/api/v1/session/exchange", json={"code": "12345678"})):
        assert answer.headers["cache-control"] == "no-store" and answer.headers["referrer-policy"] == "no-referrer"


def test_the_host_and_origin_checks_still_come_first(env):
    assert env.client.post("/api/v1/session/pairing", headers={"origin": "http://evil.example"}).status_code == 403
    assert env.client.get("/api/v1/session", headers={"host": "evil.example"}).status_code == 403
    assert env.shown == []  # a foreign page cannot make the application show a code


# ---------- nothing secret anywhere ----------

def test_no_answer_and_no_event_holds_a_token_or_a_code(env):
    seen = []
    env.clock.advance(config.PAIRING_REQUEST_GAP_S)
    seen.append(env.client.post("/api/v1/session/pairing"))
    code = env.code()
    seen.append(env.client.post("/api/v1/session/exchange", json={"code": "00000000"}))
    env.clock.advance(config.PAIRING_REQUEST_GAP_S)
    token = env.sign_in()
    code = env.code()
    seen += [env.client.get("/api/v1/session", headers=Env.bearer(token)),
             env.client.get("/api/v1/session", headers=Env.bearer("wrong-token-value")),
             env.client.delete("/api/v1/session", headers=Env.bearer(token))]
    everything = "".join(a.text + str(a.headers) for a in seen[:2] + seen[3:]) + json.dumps(env.events.items)
    assert token not in everything and "wrong-token-value" not in everything
    assert code not in everything
    assert env.events.items  # and events were in fact recorded
