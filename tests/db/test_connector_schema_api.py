"""M4.2 through the real HTTP layer: snapshots, changes, freshness, selections and the schema context. Authentication,
ownership, error codes, body limits, the security log, and that no credential is in any answer."""

# ruff: noqa: F811  (the fixtures are imported from test_connector_service and then named as arguments)

import json

import pytest

pytest.importorskip("cryptography")

from rag import config  # noqa: E402
from tests.connector_support import PASSWORD  # noqa: E402
from tests.db.test_connector_api import BASE, create, driver, env, keys  # noqa: E402, F401
from tests.db.test_connector_service import admin  # noqa: E402
from tests.db.test_connector_snapshots import CASES, SAMPLE  # noqa: E402

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def clean(database):  # the same clean-up as the other connector API tests (their autouse fixture is not imported)
    with admin() as conn:
        conn.execute("DELETE FROM connections")
        conn.execute("DELETE FROM principals WHERE kind <> 'local-owner'")
    yield
    with admin() as conn:
        conn.execute("DELETE FROM connections")
        conn.execute("DELETE FROM principals WHERE kind <> 'local-owner'")


@pytest.fixture
def cid(env):
    made = create(env)
    assert env.call("POST", f"{BASE}/{made['id']}/schema/discover").status_code == 200
    return made["id"]


def table(s, o):
    return {"kind": "table", "schema": s, "object": o}


def put(env, cid, items, **extra):
    return env.call("PUT", f"{BASE}/{cid}/schema/selections", json={"items": items, **extra})


# ---------- the routes ----------


def test_discovery_answers_with_its_snapshot(env, cid):
    again = env.call("POST", f"{BASE}/{cid}/schema/discover").json()
    assert again["snapshot"]["changed"] is False and again["snapshot"]["version"] == 1
    listed = env.call("GET", f"{BASE}/{cid}/schema/snapshots").json()["snapshots"]
    assert len(listed) == 1 and listed[0]["is_current"] is True and listed[0]["objects_count"] == 6
    one = env.call("GET", f"{BASE}/{cid}/schema/snapshots/{listed[0]['id']}")
    assert one.status_code == 200 and {s["schema_name"] for s in one.json()["schemas"]} == {"hr", "sales"}
    assert env.call("GET", f"{BASE}/{cid}/schema/snapshots/not-an-id").json() == {
        "error": {"code": "snapshot_not_found"}
    }
    assert env.call("GET", f"{BASE}/{cid}/schema/snapshots/00000000-0000-0000-0000-000000000000").status_code == 404


def test_changes_are_listed_filtered_and_validated(env, cid, driver):
    driver.sample = CASES["column added"](CASES["table added"](SAMPLE))
    assert env.call("POST", f"{BASE}/{cid}/schema/discover").json()["snapshot"]["changes"] == 2
    everything = env.call("GET", f"{BASE}/{cid}/schema/changes").json()
    assert everything["total"] == 2 and everything["summary"] == {"added": 2}
    only = env.call("GET", f"{BASE}/{cid}/schema/changes?object_type=column&version=2").json()
    assert [c["sub_name"] for c in only["changes"]] == ["email"]
    assert env.call("GET", f"{BASE}/{cid}/schema/changes?since_version=2").json()["total"] == 0
    assert env.call("GET", f"{BASE}/{cid}/schema/changes?object_type=planet").json() == {
        "error": {"code": "invalid_request"}
    }
    assert env.call("GET", f"{BASE}/{cid}/schema/changes?version=abc").status_code == 422
    assert env.call("GET", f"{BASE}/{cid}/schema/changes?limit=-5&offset=-1").status_code == 200


def test_selections_are_read_written_and_conflicts_are_409(env, cid):
    assert env.call("GET", f"{BASE}/{cid}/schema/selections").json() == {"revision": 0, "items": [], "unavailable": 0}
    saved = put(env, cid, [table("sales", "customer")], base_revision=0)
    assert saved.status_code == 200 and saved.json() == {"revision": 1, "items": 1}
    assert put(env, cid, [], base_revision=0).json() == {"error": {"code": "selection_conflict"}}
    assert put(env, cid, [table("sales", "nope")]).json() == {
        "error": {"code": "object_not_found", "reason": "not_in_snapshot"}
    }
    bad = put(env, cid, [{"kind": "table", "schema": "sales", "object": "customer", "mode": "exclude"}])
    assert bad.status_code == 400 and bad.json()["error"] == {
        "code": "invalid_selection",
        "reason": "exclude_without_scope",
    }
    assert env.call("GET", f"{BASE}/{cid}/schema/selections").json()["revision"] == 1


def test_malformed_bodies_are_422_by_field_name_only(env, cid):
    for body in ({"items": "x"}, {"items": [], "base_revision": "1"}, {"items": [], "extra": 1}, {}):
        answer = env.call("PUT", f"{BASE}/{cid}/schema/selections", json=body)
        assert answer.status_code == 422 and answer.json()["error"]["code"] == "invalid_request"
    assert env.call("POST", f"{BASE}/{cid}/schema/changes/acknowledge", json={"version": "1"}).status_code == 422
    assert env.call("POST", f"{BASE}/{cid}/schema/changes/acknowledge", json={}).status_code == 422


def test_too_many_selection_items_are_refused_and_nothing_is_stored(env, cid, monkeypatch):
    items = [table("sales", f"t{n}") for n in range(config.CONNECTOR_MAX_SELECTION_ITEMS + 1)]
    answer = put(env, cid, items)
    assert answer.status_code == 422 and answer.json()["error"]["fields"] == ["items"]
    assert env.call("GET", f"{BASE}/{cid}/schema/selections").json()["revision"] == 0


def test_a_selection_body_has_its_own_larger_limit_and_other_routes_keep_the_small_one(env, cid):
    over = env.client.request(
        "PUT",
        f"{BASE}/{cid}/schema/selections",
        content=json.dumps({"items": [], "pad": "x" * (9 * 1024 * 1024)}),
        headers={**env.auth, "Content-Type": "application/json"},
    )
    assert over.status_code == 413 and over.json() == {"error": {"code": "request_too_large"}}
    mid = env.client.request(
        "PUT",
        f"{BASE}/{cid}/descriptions",
        content=json.dumps({"description": "x" * (300 * 1024)}),
        headers={**env.auth, "Content-Type": "application/json"},
    )
    assert mid.status_code == 413
    # about a megabyte of items is accepted by the size check (then refused by what it says, not by its size)
    wide = [table("sales", "x" * 200) for _ in range(4000)]
    assert (
        put(env, cid, wide).status_code == 400
    )  # refused for what it says (the same item 4000 times), not for its size


def test_freshness_acknowledge_and_the_context_over_http(env, cid, driver):
    put(env, cid, [table("sales", "customer")])
    assert env.call("GET", f"{BASE}/{cid}/schema/freshness").json()["state"] == "current"
    driver.sample = CASES["column added"](SAMPLE)
    env.call("POST", f"{BASE}/{cid}/schema/discover")
    state = env.call("GET", f"{BASE}/{cid}/schema/freshness").json()
    assert state["state"] == "stale" and state["reasons"] == ["unreviewed_changes"]
    refused = env.call("GET", f"{BASE}/{cid}/schema/context")
    assert refused.status_code == 409 and refused.json() == {"error": {"code": "context_stale", "reason": "stale"}}
    looked = env.call("GET", f"{BASE}/{cid}/schema/context?allow_stale=true").json()
    assert looked["status"] == "stale" and looked["usable"] is False
    assert env.call("POST", f"{BASE}/{cid}/schema/changes/acknowledge", json={"version": 1}).json() == {
        "error": {"code": "snapshot_changed"}
    }
    assert (
        env.call("POST", f"{BASE}/{cid}/schema/changes/acknowledge", json={"version": 2}).json()["state"] == "current"
    )
    ready = env.call("GET", f"{BASE}/{cid}/schema/context").json()
    assert ready["usable"] is True and ready["status"] == "ok" and ready["format"] == 1
    small = env.call("GET", f"{BASE}/{cid}/schema/context?max_chars=50")
    assert small.status_code == 400 and small.json() == {"error": {"code": "invalid_request"}}
    assert env.call("GET", f"{BASE}/{cid}/schema/context?max_chars=abc").status_code == 422


def test_the_descriptions_alias_and_a_description_with_a_secret(env, cid):
    assert (
        env.call("GET", f"{BASE}/{cid}/schema/descriptions").json()
        == env.call("GET", f"{BASE}/{cid}/descriptions").json()
    )
    refused = env.call(
        "PUT", f"{BASE}/{cid}/descriptions", json={"target_kind": "database", "description": f"pw {PASSWORD}"}
    )
    assert refused.status_code == 400 and refused.json() == {"error": {"code": "description_contains_secret"}}


# ---------- who may and may not ----------


def test_the_new_routes_of_another_owner_are_not_found(env, cid):
    with admin() as conn:
        other = str(conn.execute("INSERT INTO principals (kind) VALUES ('user') RETURNING id").fetchone()["id"])
    theirs = env.service.create(
        other, {**__import__("tests.connector_support", fromlist=["PAYLOAD"]).PAYLOAD, "name": "Theirs"}
    )
    env.service.discover(other, theirs["id"])
    snapshot = env.service.snapshots(other, theirs["id"])[0]["id"]
    for method, suffix, kwargs in (
        ("GET", "/schema/snapshots", {}),
        ("GET", f"/schema/snapshots/{snapshot}", {}),
        ("GET", "/schema/changes", {}),
        ("POST", "/schema/changes/acknowledge", {"json": {"version": 1}}),
        ("GET", "/schema/freshness", {}),
        ("GET", "/schema/selections", {}),
        ("PUT", "/schema/selections", {"json": {"items": []}}),
        ("GET", "/schema/context", {}),
        ("GET", "/schema/descriptions", {}),
    ):
        answer = env.call(method, f"{BASE}/{theirs['id']}{suffix}", **kwargs)
        assert answer.status_code == 404 and answer.json() == {"error": {"code": "not_found"}}, (method, suffix)
    assert env.service.selections(other, theirs["id"])["revision"] == 0


def test_a_security_log_that_cannot_be_written_stops_a_selection_and_an_acknowledgement(env, cid):
    env.events.lost = True
    answers = [
        put(env, cid, [table("sales", "customer")]),
        env.call("POST", f"{BASE}/{cid}/schema/changes/acknowledge", json={"version": 1}),
    ]
    env.events.lost = False
    assert [a.status_code for a in answers] == [503, 503]
    assert env.call("GET", f"{BASE}/{cid}/schema/selections").json()["revision"] == 0


def test_the_events_of_the_new_actions_carry_fixed_words_only(env, cid, driver):
    put(env, cid, [table("sales", "customer")])
    env.call("POST", f"{BASE}/{cid}/schema/changes/acknowledge", json={"version": 1})
    events = [(i["event"], i["reason"]) for i in env.events.items][-2:]
    assert events == [("schema_selection_changed", "ok"), ("schema_changes_acknowledged", "ok")]
    assert "customer" not in json.dumps(env.events.items)


def test_no_answer_of_the_new_routes_holds_the_password_or_the_host(env, cid, driver):
    put(env, cid, [table("sales", "customer")])
    driver.sample = CASES["column added"](SAMPLE)
    env.call("POST", f"{BASE}/{cid}/schema/discover")
    texts = []
    for suffix in (
        "/schema/snapshots",
        "/schema/changes",
        "/schema/freshness",
        "/schema/selections",
        "/schema/context?allow_stale=true",
        "/descriptions",
    ):
        answer = env.call("GET", f"{BASE}/{cid}{suffix}")
        assert answer.status_code == 200, suffix
        assert answer.headers["cache-control"] == "no-store"
        texts.append(answer.text)
    joined = "\n".join(texts)
    assert PASSWORD not in joined and "db.example.org" not in joined and "credential" not in joined


# ---------- bodies are bounded as they arrive (rag/connector/bodylimit.py) ----------


def chunks(total, size=64 * 1024):
    def generate():
        for _ in range(total // size):
            yield b"x" * size

    return generate()


def test_a_misleading_content_length_cannot_slip_a_large_body_through(env, cid):
    answer = env.client.request(
        "PUT",
        f"{BASE}/{cid}/descriptions",
        content=b'{"target_kind": "database", "description": "' + b"x" * (400 * 1024) + b'"}',
        headers={**env.auth, "Content-Type": "application/json", "Content-Length": "40"},
    )
    assert answer.status_code == 413 and answer.json() == {"error": {"code": "request_too_large"}}
    assert answer.headers["cache-control"] == "no-store"
    assert env.call("GET", f"{BASE}/{cid}/descriptions").json() == {"descriptions": []}


def test_a_chunked_body_without_any_length_is_counted_on_the_small_and_the_large_route(env, cid):
    small = env.client.request("PUT", f"{BASE}/{cid}/descriptions", content=chunks(1024 * 1024), headers=env.auth)
    assert small.status_code == 413 and small.json() == {"error": {"code": "request_too_large"}}
    large = env.client.request(
        "PUT", f"{BASE}/{cid}/schema/selections", content=chunks(9 * 1024 * 1024), headers=env.auth
    )
    assert large.status_code == 413 and large.json() == {"error": {"code": "request_too_large"}}
    assert env.call("GET", f"{BASE}/{cid}/schema/selections").json()["revision"] == 0


def test_an_invalid_content_length_is_a_400_in_the_connector_envelope(env, cid):
    for value in ("abc", "-1", "1e3", "5 5"):
        answer = env.client.request(
            "POST", f"{BASE}/{cid}/test", headers={**env.auth, "Content-Length": value}, content=b"{}"
        )
        assert answer.status_code == 400 and answer.json() == {"error": {"code": "invalid_content_length"}}, value


def test_without_a_session_the_large_limit_does_not_apply(env, cid):
    """The large body is not even read by an anonymous caller: 413, not 401 after megabytes were buffered."""
    answer = env.client.request(
        "PUT",
        f"{BASE}/{cid}/schema/selections",
        content=json.dumps({"items": [], "pad": "x" * (400 * 1024)}),
        headers={"Content-Type": "application/json"},
    )
    assert answer.status_code == 413
    small = env.client.request("PUT", f"{BASE}/{cid}/schema/selections", json={"items": []})
    assert small.status_code == 401


def test_a_body_at_the_limit_of_the_selection_route_is_still_accepted_by_size(env, cid):
    items = [table("sales", f"t{n}") for n in range(6000)]  # about 330 KB: over the small limit, under the large
    body = json.dumps({"items": items})
    assert len(body) > 256 * 1024
    answer = env.client.request(
        "PUT", f"{BASE}/{cid}/schema/selections", content=body, headers={**env.auth, "Content-Type": "application/json"}
    )
    assert answer.status_code == 404 and answer.json()["error"]["code"] == "object_not_found"  # not a size error


def test_absurd_numbers_in_the_query_are_never_a_server_error(env, cid):
    huge = "9" * 40
    for query in (
        f"objects?limit={huge}",
        f"objects?offset={huge}",
        f"relationships?limit={huge}",
        f"relationships?offset=-{huge}",
        f"changes?version={huge}",
        f"changes?since_version={huge}",
        f"changes?limit={huge}",
        f"changes?offset={huge}",
        "changes?offset=-9223372036854775809",
        f"context?max_chars={huge}",
        f"context?max_chars={huge}&allow_stale=true",
    ):
        answer = env.call("GET", f"{BASE}/{cid}/schema/{query}")
        assert answer.status_code < 500, (query, answer.status_code, answer.text)
    for body in ({"version": int(huge)}, {"version": -1}):
        answer = env.call("POST", f"{BASE}/{cid}/schema/changes/acknowledge", json=body)
        assert answer.status_code < 500, (body, answer.status_code)
    answer = env.call("PUT", f"{BASE}/{cid}/schema/selections", json={"items": [], "base_revision": int(huge)})
    assert answer.status_code < 500


def test_nul_and_odd_characters_in_names_and_texts_are_never_a_server_error(env, cid):
    nul = "a\x00b"
    for query in (
        "object?schema=a%00b&name=x",
        "object?schema=x&name=a%00b",
        "objects?q=a%00b",
        "objects?schema=a%00b",
        "changes?object_type=a%00b",
        "changes?change_type=a%00b",
    ):
        answer = env.call("GET", f"{BASE}/{cid}/schema/{query}")
        assert answer.status_code < 500, (query, answer.status_code, answer.text)
    for kind in ("database", "schema", "table", "column"):
        answer = env.call(
            "PUT",
            f"{BASE}/{cid}/descriptions",
            json={"target_kind": kind, "schema_name": nul, "object_name": nul, "column_name": nul, "description": nul},
        )
        assert answer.status_code < 500, (kind, answer.status_code, answer.text)
    answer = env.call("PUT", f"{BASE}/{cid}/schema/selections", json={"items": [{"kind": "schema", "schema": nul}]})
    assert answer.status_code < 500
    answer = env.call("PATCH", f"{BASE}/{cid}", json={"name": nul})
    assert answer.status_code < 500
    lone = "\ud800"  # a lone surrogate cannot be encoded as UTF-8
    answer = env.client.request(
        "PUT",
        f"{BASE}/{cid}/descriptions",
        content=json.dumps({"target_kind": "database", "description": lone}),
        headers={**env.auth, "Content-Type": "application/json"},
    )
    assert answer.status_code < 500
