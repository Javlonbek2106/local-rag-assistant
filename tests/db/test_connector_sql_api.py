"""M4.3 through the real HTTP layer: POST /api/v1/connections/{id}/sql/generate with a fake language model.
Session, ownership, the error envelope and statuses, bodies, and that the answer is a proposal that was neither
checked nor run."""

import json

import pytest

pytest.importorskip("cryptography")

from rag.connector import sqlgen  # noqa: E402
from rag.connector.errors import ModelError  # noqa: E402
from tests.connector_support import PASSWORD  # noqa: E402
from tests.db.test_connector_api import BASE, create, driver, env, keys  # noqa: E402, F401
from tests.db.test_connector_service import admin  # noqa: E402
from tests.db.test_connector_snapshots import CASES, SAMPLE  # noqa: E402
from tests.db.test_connector_sqlgen_service import GOOD, TODAY, FakeModel  # noqa: E402

# ruff: noqa: F811

pytestmark = pytest.mark.db


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
def model(env):
    fake = FakeModel()
    env.service._sql_completer = fake
    env.service._today = lambda: TODAY
    return fake


@pytest.fixture
def cid(env, model):
    made = create(env)
    assert env.call("POST", f"{BASE}/{made['id']}/schema/discover").status_code == 200
    selected = {"items": [{"kind": "table", "schema": "sales", "object": "customer"}], "base_revision": 0}
    assert env.call("PUT", f"{BASE}/{made['id']}/schema/selections", json=selected).status_code == 200
    return made["id"]


def generate(env, cid, question="How many customers are there?", **kwargs):
    return env.call("POST", f"{BASE}/{cid}/sql/generate", json={"question": question}, **kwargs)


def test_a_question_gets_a_proposed_query_that_is_marked_not_checked_and_not_run(env, cid, model, driver):
    calls = list(driver.calls)
    answer = generate(env, cid)
    assert answer.status_code == 200 and answer.headers["cache-control"] == "no-store"
    body = answer.json()
    assert body["sql"] == GOOD["sql"] and body["status"] == "generated" and body["dialect"] == "PostgreSQL"
    assert body["validation"] == "not_performed" and body["executed"] is False and body["connection_id"] == cid
    assert body["context"]["state"] == "current" and body["context"]["snapshot_version"] == 1
    assert body["model"] == "fake-model" and body["prompt"]["template_version"] == sqlgen.TEMPLATE_VERSION
    assert driver.calls == calls  # the external database was not touched


def test_the_answer_and_the_log_hold_no_credential_and_no_text_of_the_question(env, cid, model, caplog):
    caplog.set_level("DEBUG")
    answer = generate(env, cid, "Secret phrase QQX9?")
    assert answer.status_code == 200
    for secret in (PASSWORD, "db.example.org", "reader"):
        assert secret not in answer.text and secret not in model.prompt
    assert "QQX9" not in caplog.text and PASSWORD not in caplog.text


def test_the_route_needs_a_session_and_no_step_up(env, cid, model):
    anonymous = env.client.post(f"{BASE}/{cid}/sql/generate", json={"question": "q"})
    assert anonymous.status_code == 401 and anonymous.json() == {"error": {"code": "unauthenticated"}}
    assert generate(env, cid).status_code == 200  # a plain session is enough: nothing is changed
    assert model.calls  # and the anonymous call never reached the model


def test_another_owners_connection_is_404_and_the_model_is_not_asked(env, cid, model):
    with admin() as conn:
        other = str(conn.execute("INSERT INTO principals (kind) VALUES ('user') RETURNING id").fetchone()["id"])
    theirs = env.service.create(
        other, {**__import__("tests.connector_support", fromlist=["PAYLOAD"]).PAYLOAD, "name": "T"}
    )
    answer = generate(env, theirs["id"])
    assert answer.status_code == 404 and answer.json() == {"error": {"code": "not_found"}} and model.calls == []
    assert generate(env, "not-an-id").status_code == 404 and model.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"question": ""},
        {"question": "x" * 2001},
        {"question": 5},
        {"question": "q", "extra": 1},
        {"question": "q", "owner_principal_id": "x"},
    ],
)
def test_a_malformed_body_is_a_422_by_field_name_only(env, cid, model, body):
    answer = env.call("POST", f"{BASE}/{cid}/sql/generate", json=body)
    assert answer.status_code == 422 and answer.json()["error"]["code"] == "invalid_request" and model.calls == []
    assert "x" * 100 not in answer.text


def test_a_question_that_is_only_blanks_is_a_400_with_its_reason(env, cid, model):
    answer = generate(env, cid, "   \n ")
    assert answer.status_code == 400 and answer.json() == {"error": {"code": "invalid_question", "reason": "empty"}}


def test_a_huge_body_is_refused_before_it_is_read(env, cid, model):
    answer = env.call("POST", f"{BASE}/{cid}/sql/generate", json={"question": "x" * (400 * 1024)})
    assert answer.status_code == 413 and answer.json() == {"error": {"code": "request_too_large"}} and model.calls == []


def test_a_stale_context_is_a_409_with_the_state_as_its_reason(env, cid, model, driver):
    driver.sample = CASES["column added"](SAMPLE)
    env.call("POST", f"{BASE}/{cid}/schema/discover")
    answer = generate(env, cid)
    assert answer.status_code == 409 and answer.json() == {"error": {"code": "context_stale", "reason": "stale"}}
    assert model.calls == []
    # the page can show why: freshness says it
    assert env.call("GET", f"{BASE}/{cid}/schema/freshness").json()["state"] == "stale"


def test_an_empty_selection_and_a_disabled_connection_have_their_own_answers(env, cid, model):
    env.call("PUT", f"{BASE}/{cid}/schema/selections", json={"items": [], "base_revision": 1})
    assert generate(env, cid).json() == {"error": {"code": "context_empty"}}
    env.call("POST", f"{BASE}/{cid}/disable")
    assert generate(env, cid).json() == {"error": {"code": "connection_disabled"}} and model.calls == []


@pytest.mark.parametrize(
    "failure,status,code,reason",
    [
        (ModelError("llm_timeout"), 504, "generation_timeout", "llm_timeout"),
        (ModelError("llm_unavailable"), 503, "generation_unavailable", "llm_unavailable"),
        (ModelError("llm_error"), 502, "generation_failed", "llm_error"),
        (RuntimeError(f"leaks {PASSWORD}"), 502, "generation_failed", "llm_error"),
    ],
)
def test_a_failure_of_the_model_is_a_structured_error_with_nothing_of_its_own_text(
    env, cid, model, failure, status, code, reason
):
    model.error = failure
    answer = generate(env, cid)
    assert answer.status_code == status and answer.json() == {"error": {"code": code, "reason": reason}}
    assert PASSWORD not in answer.text


@pytest.mark.parametrize(
    "text,code",
    [("", "empty_model_output"), ("not json", "malformed_model_output"), ('{"sql": 3}', "malformed_model_output")],
)
def test_an_empty_or_malformed_answer_is_a_502(env, cid, model, text, code):
    model.text = text
    answer = generate(env, cid)
    assert answer.status_code == 502 and answer.json()["error"]["code"] == code


def test_the_proposal_is_returned_even_when_it_is_not_valid_sql_because_nothing_checks_it_yet(env, cid, model):
    model.text = json.dumps({**GOOD, "sql": "DROP TABLE sales.customer; SELECT no_such FROM nowhere"})
    body = generate(env, cid).json()
    assert body["sql"].startswith("DROP TABLE") and body["validation"] == "not_performed" and body["executed"] is False


def test_there_is_no_route_that_runs_a_query(env):
    paths = [p for p in env.app.openapi()["paths"] if p.startswith(BASE)]
    assert not [p for p in paths if any(word in p for word in ("execute", "run", "query"))]
    assert [p for p in paths if p.endswith("/sql/generate")]
