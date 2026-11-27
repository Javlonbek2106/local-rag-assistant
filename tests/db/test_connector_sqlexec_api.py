"""M4.4 through the real HTTP layer: POST /sql/validate and /sql/execute. Session, ownership, the error envelope,
bodies, and that nothing is run without an explicit execute call. The external database is a fake driver."""

import json

import pytest

pytest.importorskip("cryptography")
pytest.importorskip("sqlglot")

from rag.connector.errors import DriverError, ModelError  # noqa: E402
from tests.connector_support import PASSWORD, PAYLOAD  # noqa: E402
from tests.db.test_connector_api import BASE, create, driver, env, keys  # noqa: E402, F401
from tests.db.test_connector_service import admin  # noqa: E402
from tests.db.test_connector_snapshots import CASES, SAMPLE  # noqa: E402
from tests.db.test_connector_sqlgen_service import FakeModel  # noqa: E402

# ruff: noqa: F811

pytestmark = pytest.mark.db
GOOD = "SELECT c.id, c.name FROM sales.customer c JOIN sales.order_line o ON o.customer_id = c.id"


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
def cid(env):
    made = create(env)
    assert env.call("POST", f"{BASE}/{made['id']}/schema/discover").status_code == 200
    items = [
        {"kind": "table", "schema": "sales", "object": "customer"},
        {"kind": "table", "schema": "sales", "object": "order_line"},
    ]
    assert (
        env.call("PUT", f"{BASE}/{made['id']}/schema/selections", json={"items": items, "base_revision": 0}).status_code
        == 200
    )
    return made["id"]


def validate(env, cid, sql=GOOD):
    return env.call("POST", f"{BASE}/{cid}/sql/validate", json={"sql": sql})


def execute(env, cid, sql=GOOD, **extra):
    return env.call("POST", f"{BASE}/{cid}/sql/execute", json={"sql": sql, **extra})


def runs(driver):
    return [c for c in driver.calls if c[0] == "run"]


def test_validate_answers_with_the_verdict_the_canonical_text_the_limits_and_runs_nothing(env, cid, driver):
    answer = validate(env, cid)
    assert answer.status_code == 200 and answer.headers["cache-control"] == "no-store"
    body = answer.json()
    assert (body["status"], body["execution_allowed"], body["executed"], body["validation"]) == (
        "valid",
        True,
        False,
        "passed",
    )
    assert (
        body["normalized_sql"].startswith("SELECT")
        and body["limits"]["max_rows"] >= 1
        and body["context"]["state"] == "current"
    )
    assert runs(driver) == []
    assert PASSWORD not in answer.text and "db.example.org" not in answer.text


def test_a_rejected_query_is_a_200_with_errors_for_validation_and_a_422_with_details_for_execution(env, cid, driver):
    body = validate(env, cid, "DROP TABLE sales.customer").json()
    assert (
        body["status"] == "invalid"
        and body["execution_allowed"] is False
        and body["errors"][0]["code"] == "ddl_not_allowed"
    )
    answer = execute(env, cid, "DROP TABLE sales.customer")
    assert answer.status_code == 422
    error = answer.json()["error"]
    assert (
        error["code"] == "sql_rejected"
        and error["reason"] == "ddl_not_allowed"
        and error["details"][0]["code"] == "ddl_not_allowed"
    )
    assert runs(driver) == []


def test_execute_runs_the_query_and_answers_with_a_bounded_result_and_no_credential(env, cid, driver):
    answer = execute(env, cid)
    assert answer.status_code == 200 and answer.headers["cache-control"] == "no-store"
    body = answer.json()
    assert (
        body["status"] == "executed"
        and body["rows"] == [[1], [2]]
        and body["row_count"] == 2
        and body["truncated"] is False
    )
    assert body["executed_sql"] == validate(env, cid).json()["normalized_sql"] and len(runs(driver)) == 1
    assert PASSWORD not in answer.text and "db.example.org" not in answer.text and "reader" not in answer.text


def test_the_routes_need_a_session_and_nothing_is_run_for_an_anonymous_caller(env, cid, driver):
    for suffix in ("validate", "execute"):
        answer = env.client.post(f"{BASE}/{cid}/sql/{suffix}", json={"sql": GOOD})
        assert answer.status_code == 401 and answer.json() == {"error": {"code": "unauthenticated"}}
    assert runs(driver) == []


def test_another_owners_connection_is_404_for_both_routes(env, cid, driver):
    with admin() as conn:
        other = str(conn.execute("INSERT INTO principals (kind) VALUES ('user') RETURNING id").fetchone()["id"])
    theirs = env.service.create(other, {**PAYLOAD, "name": "Theirs"})
    for suffix in ("validate", "execute"):
        answer = env.call("POST", f"{BASE}/{theirs['id']}/sql/{suffix}", json={"sql": GOOD})
        assert answer.status_code == 404 and answer.json() == {"error": {"code": "not_found"}}
    assert runs(driver) == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"sql": ""},
        {"sql": 5},
        {"sql": "x" * 100001},
        {"sql": "SELECT 1", "extra": 1},
        {"sql": "SELECT 1", "owner_principal_id": "x"},
        {"sql": "SELECT 1", "max_rows": 0},
        {"sql": "SELECT 1", "max_rows": "5"},
        {"sql": "SELECT 1", "max_rows": 10**40},
        {"sql": "SELECT 1", "expected": {"selection_revision": -1}},
        {"sql": "SELECT 1", "expected": {"target": "x" * 100}},
        {"sql": "SELECT 1", "expected": {"unknown": 1}},
    ],
)
def test_a_malformed_body_is_a_422_by_field_name_only_and_nothing_is_run(env, cid, driver, body):
    answer = env.call("POST", f"{BASE}/{cid}/sql/execute", json=body)
    assert answer.status_code == 422 and answer.json()["error"]["code"] == "invalid_request" and runs(driver) == []
    assert "x" * 50 not in answer.text


def test_a_huge_body_is_refused_before_it_is_read(env, cid, driver):
    for suffix in ("validate", "execute"):
        answer = env.call("POST", f"{BASE}/{cid}/sql/{suffix}", json={"sql": "SELECT '" + "x" * (400 * 1024) + "'"})
        assert answer.status_code == 413 and answer.json() == {"error": {"code": "request_too_large"}}
    assert runs(driver) == []


def test_a_stale_schema_is_a_409_for_both_routes(env, cid, driver):
    driver.sample = CASES["column added"](SAMPLE)
    env.call("POST", f"{BASE}/{cid}/schema/discover")
    for call in (validate, execute):
        answer = call(env, cid)
        assert answer.status_code == 409 and answer.json() == {"error": {"code": "context_stale", "reason": "stale"}}
    assert runs(driver) == []


def test_what_the_page_saw_must_still_be_true_or_the_run_is_refused(env, cid, driver):
    seen = validate(env, cid).json()["context"]
    expected = {
        "snapshot_fingerprint": seen["snapshot_fingerprint"],
        "selection_revision": seen["selection_revision"],
        "target": seen["target"],
    }
    assert execute(env, cid, expected=expected).status_code == 200
    changed = {**expected, "selection_revision": seen["selection_revision"] + 1}
    answer = execute(env, cid, expected=changed)
    assert (
        answer.status_code == 409 and answer.json() == {"error": {"code": "context_changed"}} and len(runs(driver)) == 1
    )


@pytest.mark.parametrize(
    "failure,status,code,reason",
    [
        (DriverError("timeout"), 504, "execution_timeout", None),
        (DriverError("account_can_write"), 409, "account_can_write", None),
        (DriverError("permission_denied"), 403, "permission_denied", None),
        (DriverError("query_error"), 422, "query_failed", None),
        (DriverError("unreachable"), 502, "execution_failed", "unreachable"),
        (RuntimeError(f"boom {PASSWORD}"), 502, "execution_failed", "failed"),
    ],
)
def test_a_failure_of_the_run_is_a_structured_error_with_nothing_of_the_databases_text(
    env, cid, driver, failure, status, code, reason
):
    driver.run_error = failure
    answer = execute(env, cid)
    assert answer.status_code == status
    assert answer.json() == {"error": {"code": code, **({"reason": reason} if reason else {})}}
    assert PASSWORD not in answer.text


def test_a_cut_result_is_reported_in_the_answer(env, cid, driver):
    from rag.connector.drivers import QueryResult

    driver.run_result = QueryResult([{"name": "id", "type": "int"}], [[1]], "bytes", 0.1)
    body = execute(env, cid).json()
    assert (
        body["truncated"] is True
        and body["truncated_by"] == "bytes"
        and body["warnings"][0]["code"] == "result_truncated"
    )


def test_a_generated_proposal_is_not_run_by_generating_it(env, cid, driver):
    model = FakeModel()
    model.text = json.dumps(
        {"sql": "DELETE FROM sales.customer", "explanation": "", "assumptions": [], "ambiguities": []}
    )
    env.service._sql_completer = model
    answer = env.call("POST", f"{BASE}/{cid}/sql/generate", json={"question": "delete everyone"})
    assert answer.status_code == 200 and answer.json()["executed"] is False and runs(driver) == []
    proposal = answer.json()["sql"]
    assert validate(env, cid, proposal).json()["execution_allowed"] is False  # and it would not pass validation anyway
    assert execute(env, cid, proposal).status_code == 422 and runs(driver) == []


def test_there_is_no_route_that_takes_a_query_and_skips_the_checks(env):
    paths = [p for p in env.app.openapi()["paths"] if p.startswith(BASE)]
    assert sorted(p for p in paths if "/sql/" in p and "generate" not in p) == [
        "/api/v1/connections/{connection_id}/sql/execute",
        "/api/v1/connections/{connection_id}/sql/validate",
    ]
    assert not [p for p in paths if any(word in p for word in ("raw", "query", "run", "unsafe"))]


def test_absurd_and_hostile_text_in_the_sql_is_never_a_server_error(env, cid, driver):
    for sql in (
        "\x00",
        "SELECT '\x00'",
        "\ud800",
        "SELECT " + "(" * 5000,
        "SELECT " + "𝔘" * 100,
        "SELECT 1 /*",
        "'",
        "\\",
    ):
        for suffix in ("validate", "execute"):
            answer = env.client.request(
                "POST",
                f"{BASE}/{cid}/sql/{suffix}",
                content=json.dumps({"sql": sql}),
                headers={**env.auth, "Content-Type": "application/json"},
            )
            assert answer.status_code < 500, (suffix, sql[:20], answer.status_code)
    assert runs(driver) == []
    assert ModelError  # (imported for the error vocabulary of the neighbouring tests)
