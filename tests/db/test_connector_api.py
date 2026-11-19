"""M4.1b through the real HTTP layer: authentication, step-up, ownership, no credential in any answer or log.

A real PostgreSQL (throw-away schema) and the real session machinery of M4.1a; the external databases are a fake
driver.
"""

import json
import re

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("cryptography")

from rag import config, connector_wiring, db, health  # noqa: E402
from rag.api import create_app  # noqa: E402
from rag.auth.checks import SecurityChecks  # noqa: E402
from rag.auth.deps import AuthContext  # noqa: E402
from rag.auth.events import CHECK_NAMES  # noqa: E402
from rag.auth.pairing import PairingBoard  # noqa: E402
from rag.auth.principals import Principals  # noqa: E402
from rag.connector.service import Service  # noqa: E402
from tests.auth_support import Clock, Events  # noqa: E402
from tests.connector_support import PASSWORD, PAYLOAD, FakeDriver, KeyRing, internal, resolver  # noqa: E402
from tests.db.test_connector_service import admin  # noqa: E402

pytestmark = pytest.mark.db
BASE = "/api/v1/connections"
BODY = {key: value for key, value in PAYLOAD.items()}


class Env:
    def __init__(self, keys, driver, monkeypatch):
        self.clock, self.events, self.shown = Clock(), Events(), []
        self.flags = {name: True for name in CHECK_NAMES}
        checks = {name: (lambda n=name: self.flags[n]) for name in CHECK_NAMES}
        checks["security_log_writable"] = lambda: self.flags["security_log_writable"] and self.events.check()
        self.ctx = AuthContext(
            Principals(),
            self.events,
            checks=SecurityChecks(checks, self.events, self.clock),
            clock=self.clock,
            dirs=[],
            pairing=PairingBoard(self.clock, show=self.shown.append),
        )
        self.service = Service(keys.vault, self.events, resolver=resolver(), internal=internal, driver_lookup=driver)
        monkeypatch.setattr(connector_wiring, "build_service", lambda events: self.service)
        self.app = create_app(db, security=self.ctx)
        self.client = TestClient(self.app, base_url=f"http://127.0.0.1:{config.PORT}")
        self.token = None

    def code(self):
        return re.search(r"(\d{4}) (\d{4})", self.shown[-1]).expand(r"\1\2")

    def sign_in(self):
        self.clock.advance(config.PAIRING_REQUEST_GAP_S)
        assert self.client.post("/api/v1/session/pairing").status_code == 200
        answer = self.client.post("/api/v1/session/exchange", json={"code": self.code()})
        self.token = answer.json()["token"]

    def step_up(self):
        self.clock.advance(config.PAIRING_REQUEST_GAP_S)
        assert (
            self.client.post("/api/v1/session/pairing", json={"purpose": "step-up"}, headers=self.auth).status_code
            == 200
        )
        answer = self.client.post("/api/v1/session/step-up", json={"code": self.code()}, headers=self.auth)
        assert answer.status_code == 200, answer.text
        self.token = answer.json()["token"]

    @property
    def auth(self):
        return {"Authorization": f"Bearer {self.token}"}

    def call(self, method, path, **kwargs):
        return self.client.request(method, path, headers=self.auth, **kwargs)


@pytest.fixture(autouse=True)
def clean(database):
    with admin() as conn:
        conn.execute("DELETE FROM connections")
        conn.execute("DELETE FROM principals WHERE kind <> 'local-owner'")
    yield
    with admin() as conn:
        conn.execute("DELETE FROM connections")
        conn.execute("DELETE FROM principals WHERE kind <> 'local-owner'")


@pytest.fixture
def keys():
    return KeyRing()


@pytest.fixture
def driver():
    return FakeDriver()


@pytest.fixture
def env(keys, driver, storage_dir, qdrant, monkeypatch):
    monkeypatch.setattr(health, "check_qdrant", lambda: "ok")
    monkeypatch.setattr(health, "check_ollama", lambda: "ok")
    environment = Env(keys, driver, monkeypatch)
    with environment.client:
        environment.sign_in()
        yield environment


def create(env, **changes):
    env.step_up()
    answer = env.call("POST", BASE, json={**BODY, **changes})
    assert answer.status_code == 201, answer.text
    return answer.json()


# ---------- authentication and step-up ----------


def test_every_connector_route_needs_a_session(env):
    paths = [(m.upper(), p) for p, item in env.app.openapi()["paths"].items() if p.startswith(BASE) for m in item]
    assert len(paths) >= 14
    for method, path in paths:
        concrete = path.replace("{connection_id}", "00000000-0000-0000-0000-000000000000")
        answer = env.client.request(method, concrete)
        assert answer.status_code == 401, (method, path, answer.status_code)
        assert answer.json() == {"error": {"code": "unauthenticated"}}
        assert env.client.request(method, concrete, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert env.client.request(method, concrete + f"?token={env.token}").status_code == 401  # not in the query


def test_the_connector_answers_503_when_a_security_condition_fails(env):
    env.flags["db_role_unprivileged"] = False
    env.clock.advance(config.SECURITY_CHECK_TTL_S + 1)
    answer = env.call("GET", BASE)
    assert answer.status_code == 503 and answer.json() == {"error": {"code": "connector_unavailable"}}


def test_creating_a_connection_needs_a_fresh_step_up_and_without_it_nothing_is_stored(env):
    answer = env.call("POST", BASE, json=BODY)
    assert answer.status_code == 403 and answer.json() == {"error": {"code": "step_up_required"}}
    assert env.call("GET", BASE).json() == {"connections": []}


def test_changing_where_the_credential_goes_needs_a_step_up_but_a_rename_does_not(env):
    made = create(env)
    env.sign_in()  # a new session: it has no step-up
    renamed = env.call("PATCH", f"{BASE}/{made['id']}", json={"name": "Renamed", "description": "d2"})
    assert renamed.status_code == 200 and renamed.json()["name"] == "Renamed"
    for change in (
        {"host": "other.example.org"},
        {"password": "a new password 12345"},
        {"username": "x"},
        {"port": 5433},
        {"ssl_mode": "verify-ca"},
        {"database_name": "other"},
        {"provider": "mssql"},
        {"options": {"connect_timeout_s": 4}},
    ):
        refused = env.call("PATCH", f"{BASE}/{made['id']}", json=change)
        assert refused.status_code == 403 and refused.json()["error"]["code"] == "step_up_required", change
    assert env.call("GET", f"{BASE}/{made['id']}").json()["host"] == "db.example.org"
    env.step_up()
    allowed = env.call("PATCH", f"{BASE}/{made['id']}", json={"host": "other.example.org"})
    assert allowed.status_code == 200 and allowed.json()["host"] == "other.example.org"


# ---------- what the API answers ----------


def test_create_list_get_update_enable_disable_test_discover_and_delete(env, driver):
    made = create(env)
    cid = made["id"]
    assert made["name"] == "Sales" and made["status"] == "ready" and made["credential_stored"] is True
    listed = env.call("GET", BASE).json()["connections"]
    assert [c["id"] for c in listed] == [cid] and env.call("GET", f"{BASE}/{cid}").json()["id"] == cid
    assert env.call("POST", f"{BASE}/{cid}/disable").json()["status"] == "disabled"
    assert env.call("POST", f"{BASE}/{cid}/test").status_code == 409
    assert env.call("POST", f"{BASE}/{cid}/enable").json()["status"] == "ready"
    tested = env.call("POST", f"{BASE}/{cid}/test")
    assert tested.status_code == 200 and tested.json()["status"] == "ok"
    assert env.call("GET", f"{BASE}/{cid}").json()["last_test_status"] == "ok"
    found = env.call("POST", f"{BASE}/{cid}/schema/discover")
    assert found.status_code == 200 and found.json()["objects"] == 6
    assert [s["schema_name"] for s in env.call("GET", f"{BASE}/{cid}/schema").json()["schemas"]] == ["hr", "sales"]
    objects = env.call("GET", f"{BASE}/{cid}/schema/objects", params={"schema": "sales", "q": "order"}).json()
    assert objects["total"] == 3
    detail = env.call("GET", f"{BASE}/{cid}/schema/object", params={"schema": "sales", "name": "order_line"}).json()
    assert [c["column_name"] for c in detail["columns"]] == ["order_id", "line_no", "customer_id"]
    assert len(env.call("GET", f"{BASE}/{cid}/schema/relationships").json()["relationships"]) == 2
    put = env.call(
        "PUT",
        f"{BASE}/{cid}/descriptions",
        json={"target_kind": "table", "schema_name": "sales", "object_name": "customer", "description": "people"},
    )
    assert put.status_code == 200
    assert env.call("GET", f"{BASE}/{cid}/descriptions").json()["descriptions"][0]["description"] == "people"
    assert env.call("DELETE", f"{BASE}/{cid}").json() == {"deleted": True}
    assert env.call("GET", f"{BASE}/{cid}").status_code == 404


def test_a_failing_test_is_a_200_with_a_fixed_code_and_a_failing_discovery_a_502_with_a_fixed_reason(env, driver):
    from rag.connector.errors import DriverError

    cid = create(env)["id"]
    driver.test_error = DriverError("auth_failed")
    answer = env.call("POST", f"{BASE}/{cid}/test")
    assert answer.status_code == 200 and answer.json()["status"] == "failed" and answer.json()["error"] == "auth_failed"
    driver.discover_error = "timeout"
    failed = env.call("POST", f"{BASE}/{cid}/schema/discover")
    assert failed.status_code == 502 and failed.json() == {"error": {"code": "discovery_failed", "reason": "timeout"}}


def test_the_owner_cannot_be_chosen_by_the_client(env):
    env.step_up()
    answer = env.call("POST", BASE, json={**BODY, "owner_principal_id": "00000000-0000-0000-0000-000000000001"})
    assert answer.status_code == 422 and answer.json() == {
        "error": {"code": "invalid_request", "fields": ["owner_principal_id"]}
    }
    with admin() as conn:
        assert conn.execute("SELECT count(*) AS n FROM connections").fetchone()["n"] == 0


def test_a_rejected_body_is_never_echoed_back(env):
    env.step_up()
    answer = env.call("POST", BASE, json={**BODY, "port": "not a number", "password": PASSWORD})
    assert answer.status_code == 422 and PASSWORD not in answer.text and "input" not in answer.text
    assert answer.json()["error"]["fields"] == ["port"]
    broken = env.client.post(BASE, content=b"{not json", headers={**env.auth, "Content-Type": "application/json"})
    assert broken.status_code == 422 and "not json" not in broken.text


def test_the_validation_answer_of_the_rest_of_the_application_is_unchanged(env):
    answer = env.client.post("/api/ask", json={})
    assert answer.status_code == 422 and "detail" in answer.json()


def test_a_value_the_connector_refuses_has_its_own_code(env):
    env.step_up()
    for change, expected in (
        ({"host": "169.254.169.254"}, "host_blocked"),
        ({"provider": "oracle"}, "invalid_provider"),
        ({"ssl_mode": "disable"}, "tls_required"),
        ({"name": ""}, "invalid_name"),
    ):
        answer = env.call("POST", BASE, json={**BODY, **change})
        assert answer.status_code == 400 and answer.json() == {"error": {"code": expected}}, change


def test_a_duplicate_name_is_a_409(env):
    create(env)
    answer = env.call("POST", BASE, json=BODY)
    assert answer.status_code == 409 and answer.json() == {"error": {"code": "name_taken"}}


def test_a_connection_of_another_owner_is_not_found_over_http(env, keys, driver):
    with admin() as conn:
        other = str(conn.execute("INSERT INTO principals (kind) VALUES ('user') RETURNING id").fetchone()["id"])
    theirs = env.service.create(other, PAYLOAD)
    mine = create(env, name="Mine")
    assert [c["id"] for c in env.call("GET", BASE).json()["connections"]] == [mine["id"]]
    cid = theirs["id"]
    for method, suffix, kwargs in (
        ("GET", "", {}),
        ("PATCH", "", {"json": {"name": "x"}}),
        ("DELETE", "", {}),
        ("POST", "/test", {}),
        ("POST", "/enable", {}),
        ("POST", "/disable", {}),
        ("POST", "/schema/discover", {}),
        ("GET", "/schema", {}),
        ("GET", "/schema/objects", {}),
        ("GET", "/descriptions", {}),
        ("GET", "/schema/relationships", {}),
        ("GET", "/schema/object?schema=a&name=b", {}),
        ("PUT", "/descriptions", {"json": {"target_kind": "database", "description": "x"}}),
    ):
        answer = env.call(method, f"{BASE}/{cid}{suffix}", **kwargs)
        assert answer.status_code == 404 and answer.json() == {"error": {"code": "not_found"}}, (method, suffix)
    assert env.service.get(other, cid)["name"] == "Sales"


def test_a_path_that_is_not_an_id_is_a_404_not_an_error(env):
    for bad in ("nope", "1%27%3B%20DROP%20TABLE%20connections", "..%2F..%2Fsession"):
        assert env.call("GET", f"{BASE}/{bad}").status_code == 404


def test_the_answers_are_not_cached_and_do_not_leak_a_referrer(env):
    made = create(env)
    for path in (BASE, f"{BASE}/{made['id']}"):
        headers = env.call("GET", path).headers
        assert headers["cache-control"] == "no-store" and headers["referrer-policy"] == "no-referrer"


# ---------- the credential is nowhere ----------


def test_the_password_is_in_no_answer_and_no_log_and_no_stdout(env, driver, caplog, capsys):
    caplog.set_level("DEBUG")
    made = create(env)
    cid = made["id"]
    answers = [
        env.call("GET", BASE),
        env.call("GET", f"{BASE}/{cid}"),
        env.call("POST", f"{BASE}/{cid}/test"),
        env.call("POST", f"{BASE}/{cid}/schema/discover"),
        env.call("GET", f"{BASE}/{cid}/schema/objects"),
    ]
    env.step_up()
    answers.append(
        env.call("PATCH", f"{BASE}/{cid}", json={"password": "rotated-Secret-4455", "host": "x.example.org"})
    )
    from rag.connector.errors import DriverError

    driver.test_error = DriverError("auth_failed")
    answers.append(env.call("POST", f"{BASE}/{cid}/test"))
    driver.test_error = RuntimeError(f"password={PASSWORD} host=db.example.org")
    answers.append(env.call("POST", f"{BASE}/{cid}/test"))
    answers.append(env.call("POST", BASE, json={**BODY, "port": 0}))
    text = " ".join(a.text for a in answers) + json.dumps([dict(a.headers) for a in answers])
    seen = (
        text
        + caplog.text
        + capsys.readouterr().out
        + capsys.readouterr().err
        + json.dumps(env.events.items, default=str)
    )
    for secret in (PASSWORD, "rotated-Secret-4455"):
        assert secret not in seen
    assert "ciphertext" not in text and "nonce" not in text


def test_the_security_events_carry_ids_and_fixed_words_only(env, driver):
    made = create(env)
    env.call("POST", f"{BASE}/{made['id']}/test")
    env.call("POST", f"{BASE}/{made['id']}/disable")
    env.call("DELETE", f"{BASE}/{made['id']}")
    names = [(e["event"], e["reason"]) for e in env.events.items if e["event"].startswith(("connection_", "schema_"))]
    assert names == [
        ("connection_created", "ok"),
        ("connection_tested", "ok"),
        ("connection_disabled", "ok"),
        ("connection_deleted", "ok"),
    ]
    assert all(e["event_ref"] == made["id"] for e in env.events.items if e["event"].startswith("connection_"))
    assert PASSWORD not in json.dumps(env.events.items, default=str)


def test_a_security_log_that_cannot_be_written_stops_a_change_with_a_503(env):
    made = create(env)
    owner = Principals().local_owner()["id"]
    env.events.lost = True
    answers = [
        env.call("DELETE", f"{BASE}/{made['id']}"),
        env.call("POST", f"{BASE}/{made['id']}/disable"),
        env.call("PATCH", f"{BASE}/{made['id']}", json={"description": "changed"}),
    ]
    env.events.lost = False
    assert [a.status_code for a in answers] == [503, 503, 503]
    assert all(a.json() == {"error": {"code": "security_log_unavailable"}} for a in answers)
    kept = env.service.get(owner, made["id"])  # none of the three was done
    assert kept["is_enabled"] is True and kept["description"] == "the sales database"


def test_a_request_body_far_larger_than_a_connection_is_refused_before_it_is_read(env):
    env.step_up()
    huge = {**BODY, "description": "x" * (300 * 1024)}
    answer = env.call("POST", BASE, json=huge)
    assert answer.status_code == 413 and answer.json() == {"error": {"code": "request_too_large"}}
    assert env.call("GET", BASE).json() == {"connections": []}
