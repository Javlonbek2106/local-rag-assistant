"""M4.4 through the service against a real PostgreSQL: validating and running SQL, with a fake driver for the external
database (the real drivers are in tests/integration/test_sql_execution.py). What is proved: validation uses only the
owner's selected, CURRENT schema; execution validates again from scratch and runs the canonical text only; the client
cannot claim a validation; a changed schema, selection or target stops a run; nothing runs before an explicit execute
call; a security event is written before every run (no record, no run); every failure is a fixed code; limits are passed
to the driver; ownership is checked first; no credential or SQL text leaks."""

import json
import threading

import pytest

pytest.importorskip("cryptography")
pytest.importorskip("sqlglot")

from rag import config  # noqa: E402
from rag.connector.drivers import CancelToken, QueryResult  # noqa: E402
from rag.connector.errors import ConnectorError, DriverError  # noqa: E402
from rag.connector.service import Service  # noqa: E402
from tests.connector_support import PASSWORD, PAYLOAD, internal, resolver  # noqa: E402
from tests.db.test_connector_service import (  # noqa: E402, F401
    admin,
    clean,
    code,
    driver,
    events,
    keys,
    owner,
    stranger,
)
from tests.db.test_connector_snapshots import CASES, SAMPLE, discover  # noqa: E402

# ruff: noqa: F811

pytestmark = pytest.mark.db
GOOD = "SELECT c.id, c.name FROM sales.customer c JOIN sales.order_line o ON o.customer_id = c.id"
MSSQL = {
    **PAYLOAD,
    "name": "SqlServer",
    "provider": "mssql",
    "host": "localhost",
    "port": 1433,
    "database_name": "rag_ext",
    "ssl_mode": "disable",
}


@pytest.fixture
def service(keys, events, driver):
    return Service(keys.vault, events, resolver=resolver(), internal=internal, driver_lookup=driver)


@pytest.fixture
def local_service(keys, events, driver):
    return Service(keys.vault, events, resolver=resolver("127.0.0.1"), internal=internal, driver_lookup=driver)


def table(s, o):
    return {"kind": "table", "schema": s, "object": o}


@pytest.fixture
def ready(service, owner, driver):
    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    service.put_selections(owner, made["id"], [table("sales", "customer"), table("sales", "order_line")])
    return made["id"]


def runs(driver):
    return [call for call in driver.calls if call[0] == "run"]


# ---------- validation ----------


def test_a_valid_query_is_validated_against_the_selected_current_schema_and_nothing_is_run(
    service, owner, ready, driver
):
    before = list(driver.calls)
    result = service.validate_sql(owner, ready, GOOD)
    assert (result["status"], result["execution_allowed"], result["validation"], result["executed"]) == (
        "valid",
        True,
        "passed",
        False,
    )
    assert result["errors"] == [] and result["warnings"] == [] and result["dialect"] == "PostgreSQL"
    assert result["normalized_sql"].endswith("LIMIT 1001") and '"sales"."customer"' in result["normalized_sql"]
    assert [t["name"] for t in result["referenced"]["tables"]] == ["customer", "order_line"]
    context = result["context"]
    assert context["state"] == "current" and context["snapshot_version"] == 1 and context["selected_objects"] == 2
    assert (
        len(context["snapshot_fingerprint"]) == 64
        and context["selection_revision"] == 1
        and len(context["target"]) == 16
    )
    assert result["limits"] == {
        "max_rows": config.SQL_EXEC_MAX_ROWS,
        "timeout_s": config.SQL_EXEC_TIMEOUT_S,
        "max_result_bytes": config.SQL_EXEC_MAX_BYTES,
        "max_cell_chars": config.SQL_EXEC_MAX_CELL_CHARS,
    }
    assert driver.calls == before  # the external database is not contacted by a validation, not even to look it up


def test_a_rejected_query_says_why_in_structured_errors_and_may_not_be_executed(service, owner, ready):
    result = service.validate_sql(owner, ready, "DELETE FROM sales.customer")
    assert (result["status"], result["execution_allowed"], result["normalized_sql"]) == ("invalid", False, None)
    assert result["errors"][0]["code"] == "dml_not_allowed" and "message" in result["errors"][0]


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("SELECT id FROM hr.employee", "table_not_selected"),
        ("SELECT id FROM sales.nope", "unknown_table"),
        ("SELECT nope FROM sales.customer", "unknown_column"),
        ("SELECT id FROM sales.customer; DROP TABLE sales.customer", "multiple_statements"),
        ("SELECT pg_read_file('/etc/passwd')", "dangerous_function"),
        ("SELECT * INTO x FROM sales.customer", "select_into_not_allowed"),
    ],
)
def test_unselected_unknown_and_dangerous_things_are_refused_with_their_reasons(service, owner, ready, sql, expected):
    assert [e["code"] for e in service.validate_sql(owner, ready, sql)["errors"]][0] == expected


def test_an_excluded_column_does_not_exist_for_a_query_and_a_star_becomes_the_shown_columns(service, owner, driver):
    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    hidden = {"kind": "column", "schema": "sales", "object": "customer", "column": "name", "mode": "exclude"}
    service.put_selections(owner, made["id"], [table("sales", "customer"), hidden])
    refused = service.validate_sql(owner, made["id"], "SELECT c.name FROM sales.customer c")
    assert refused["errors"][0]["code"] == "unknown_column"
    star = service.validate_sql(owner, made["id"], "SELECT * FROM sales.customer")
    assert star["execution_allowed"] and '"name"' not in star["normalized_sql"] and '"id"' in star["normalized_sql"]


def test_a_view_that_is_selected_can_be_queried(service, owner, driver):
    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    service.put_selections(owner, made["id"], [table("sales", "big_orders")])
    assert service.validate_sql(owner, made["id"], "SELECT order_id FROM sales.big_orders")["execution_allowed"]


def test_a_join_not_on_a_known_relationship_is_a_warning_and_the_relationship_known_from_the_schema_is_not(
    service, owner, ready
):
    odd = service.validate_sql(
        owner, ready, "SELECT c.id FROM sales.customer c JOIN sales.order_line o ON o.line_no = c.id"
    )
    assert odd["execution_allowed"] and [w["code"] for w in odd["warnings"]] == ["join_not_on_known_relationship"]
    assert service.validate_sql(owner, ready, GOOD)["warnings"] == []


def test_sql_server_queries_are_validated_in_t_sql_for_a_connection_that_is_sql_server(local_service, owner, driver):
    made = local_service.create(owner, MSSQL)
    local_service.discover(owner, made["id"])
    local_service.put_selections(owner, made["id"], [table("sales", "customer")])
    result = local_service.validate_sql(owner, made["id"], "SELECT TOP 5 c.ID, c.NAME FROM [sales].[Customer] c")
    assert result["dialect"] == "Microsoft SQL Server (T-SQL)" and result["execution_allowed"]
    assert (
        result["normalized_sql"] == "SELECT TOP 5 [c].[id] AS [id], [c].[name] AS [name] FROM [sales].[customer] AS [c]"
    )
    assert not local_service.validate_sql(owner, made["id"], "EXEC xp_cmdshell 'dir'")["execution_allowed"]
    lenient = local_service.validate_sql(owner, made["id"], "SELECT id FROM sales.customer LIMIT 5")
    assert (
        "TOP 5" in lenient["normalized_sql"] and "LIMIT" not in lenient["normalized_sql"]
    )  # what runs is always T-SQL


@pytest.mark.parametrize("state", ["never", "failed", "stale", "empty", "disabled", "changed_database"])
def test_without_a_current_usable_schema_there_is_nothing_to_validate_against(service, owner, driver, state):
    made = service.create(owner, PAYLOAD)
    cid = made["id"]
    if state != "never":
        service.discover(owner, cid)
        service.put_selections(owner, cid, [] if state == "empty" else [table("sales", "customer")])
    if state == "failed":
        driver.fail_after, driver.discover_error = 1, "timeout"
        with pytest.raises(ConnectorError):
            service.discover(owner, cid)
    if state == "stale":
        discover(service, owner, driver, cid, CASES["column added"](SAMPLE))
    if state == "disabled":
        service.set_enabled(owner, cid, False)
    if state == "changed_database":
        service.update(owner, cid, {"database_name": "another"})
    wanted = {"empty": "context_empty", "disabled": "connection_disabled"}.get(state, "context_stale")
    for call in (service.validate_sql, service.execute_sql):
        assert code(call, owner, cid, GOOD) == wanted
    assert runs(driver) == []


def test_another_owner_gets_404_for_validation_and_execution_and_nothing_is_run_or_recorded(
    service, owner, stranger, ready, driver, events
):
    seen = len(events.items)
    for call in (service.validate_sql, service.execute_sql):
        assert code(call, stranger, ready, GOOD) == "not_found"
        assert code(call, stranger, "00000000-0000-0000-0000-000000000000", GOOD) == "not_found"
        assert code(call, stranger, "not-an-id", GOOD) == "not_found"
    assert runs(driver) == [] and len(events.items) == seen


def test_the_schema_of_one_connection_is_never_used_for_another(service, owner, driver):
    first = service.create(owner, PAYLOAD)
    service.discover(owner, first["id"])
    service.put_selections(owner, first["id"], [table("hr", "employee")])
    other = service.create(owner, {**PAYLOAD, "name": "Other"})
    driver.sample = {
        "objects": [("vault", "payroll", "table")],
        "columns": [("vault", "payroll", 1, "salary", "numeric", False)],
        "constraints": [],
        "indexes": [],
    }
    service.discover(owner, other["id"])
    service.put_selections(owner, other["id"], [table("vault", "payroll")])
    assert service.validate_sql(owner, other["id"], "SELECT salary FROM vault.payroll")["execution_allowed"]
    assert (
        service.validate_sql(owner, first["id"], "SELECT salary FROM vault.payroll")["errors"][0]["code"]
        == "unknown_table"
    )
    assert (
        service.validate_sql(owner, other["id"], "SELECT email FROM hr.employee")["errors"][0]["code"]
        == "unknown_table"
    )


def test_oversized_sql_is_refused_by_the_configured_limit(service, owner, ready, monkeypatch):
    monkeypatch.setattr(config, "SQL_INPUT_MAX_CHARS", 50)
    assert service.validate_sql(owner, ready, "SELECT " + "1," * 40 + "1")["errors"][0]["code"] == "sql_too_large"


# ---------- execution ----------


def test_nothing_runs_until_execute_is_called_not_after_generation_not_after_validation(service, owner, ready, driver):
    service.validate_sql(owner, ready, GOOD)
    service.validate_sql(owner, ready, "SELECT 1")
    assert runs(driver) == []
    service.execute_sql(owner, ready, GOOD)
    assert len(runs(driver)) == 1


def test_execution_runs_the_canonical_text_of_its_own_analysis_with_the_limits_and_returns_a_bounded_result(
    service, owner, ready, driver
):
    driver.run_result = QueryResult(
        [{"name": "id", "type": "int"}, {"name": "name", "type": "text"}], [[1, "a"], [2, "b"]], None, 0.25
    )
    validated = service.validate_sql(owner, ready, GOOD)
    result = service.execute_sql(owner, ready, GOOD)
    (call,) = runs(driver)
    assert call[3] == validated["normalized_sql"] == result["executed_sql"]  # exactly what the validator wrote
    assert call[2] == PASSWORD  # the decrypted password goes to the driver and nowhere else
    limits = call[4]
    assert (limits.max_rows, limits.timeout_s, limits.max_bytes, limits.max_cell_chars) == (
        config.SQL_EXEC_MAX_ROWS,
        config.SQL_EXEC_TIMEOUT_S,
        config.SQL_EXEC_MAX_BYTES,
        config.SQL_EXEC_MAX_CELL_CHARS,
    )
    assert result["status"] == "executed" and result["row_count"] == 2 and result["rows"] == [[1, "a"], [2, "b"]]
    assert (
        [c["name"] for c in result["columns"]] == ["id", "name"]
        and result["truncated"] is False
        and result["truncated_by"] is None
    )
    assert result["validation"] == "passed" and result["duration_ms"] == 250 and result["connection_id"] == ready


def test_a_result_that_was_cut_says_so_everywhere(service, owner, ready, driver):
    driver.run_result = QueryResult([{"name": "id", "type": "int"}], [[1]], "rows", 0.1)
    result = service.execute_sql(owner, ready, GOOD)
    assert result["truncated"] is True and result["truncated_by"] == "rows"
    assert [w["code"] for w in result["warnings"]] == ["result_truncated"]
    driver.run_result = QueryResult([{"name": "id", "type": "int"}], [[1]], "bytes", 0.1)
    assert service.execute_sql(owner, ready, GOOD)["truncated_by"] == "bytes"


def test_a_page_can_ask_for_fewer_rows_but_never_for_more_than_the_limit(service, owner, ready, driver, monkeypatch):
    service.execute_sql(owner, ready, GOOD, max_rows=5)
    assert runs(driver)[-1][4].max_rows == 5 and runs(driver)[-1][3].endswith(
        "LIMIT 6"
    )  # one more, to see that there is more
    service.execute_sql(owner, ready, GOOD, max_rows=10**9)
    assert runs(driver)[-1][4].max_rows == config.SQL_EXEC_MAX_ROWS
    monkeypatch.setattr(config, "SQL_EXEC_MAX_ROWS", 50000)
    monkeypatch.setattr(config, "SQL_EXEC_MAX_ROWS_HARD", 200)
    service.execute_sql(owner, ready, GOOD, max_rows=10**9)
    assert runs(driver)[-1][4].max_rows == 200  # the hard limit wins over a generous setting


def test_the_client_cannot_claim_a_validation_the_sql_is_always_analysed_again(service, owner, ready, driver):
    assert service.validate_sql(owner, ready, GOOD)["execution_allowed"]
    for bad in ("DELETE FROM sales.customer", "SELECT id FROM hr.employee", "SELECT 1; SELECT 2", "SELECT pg_sleep(9)"):
        error = pytest.raises(ConnectorError, service.execute_sql, owner, ready, bad).value
        assert (
            (error.code, error.status) == ("sql_rejected", 422)
            and error.details
            and error.reason == error.details[0]["code"]
        )
    assert runs(driver) == []


def test_sql_changed_after_validation_is_analysed_as_what_it_now_is(service, owner, ready, driver):
    service.validate_sql(owner, ready, "SELECT id FROM sales.customer")
    service.execute_sql(owner, ready, "SELECT id FROM sales.customer WHERE id > 1")
    assert (
        runs(driver)[-1][3]
        == service.validate_sql(owner, ready, "SELECT id FROM sales.customer WHERE id > 1")["normalized_sql"]
    )
    assert (
        code(service.execute_sql, owner, ready, "SELECT id FROM sales.customer; DELETE FROM sales.customer")
        == "sql_rejected"
    )


def test_a_schema_that_changed_between_validation_and_execution_stops_the_run(service, owner, ready, driver):
    validated = service.validate_sql(owner, ready, GOOD)
    discover(
        service, owner, driver, ready, CASES["column added"](SAMPLE)
    )  # a new column of a selected table, unreviewed
    assert code(service.execute_sql, owner, ready, GOOD) == "context_stale" and runs(driver) == []
    service.acknowledge(owner, ready, 2)  # the owner reviews: current again, but it is a different snapshot
    expected = {"snapshot_fingerprint": validated["context"]["snapshot_fingerprint"]}
    assert code(service.execute_sql, owner, ready, GOOD, expected) == "context_changed" and runs(driver) == []
    service.execute_sql(owner, ready, GOOD)  # without the claim the run is validated against what is there now
    assert len(runs(driver)) == 1


def test_a_changed_selection_or_target_between_validation_and_execution_is_refused_when_the_page_says_what_it_saw(
    service, owner, ready, driver
):
    validated = service.validate_sql(owner, ready, GOOD)["context"]
    expected = {
        "snapshot_fingerprint": validated["snapshot_fingerprint"],
        "selection_revision": validated["selection_revision"],
        "target": validated["target"],
    }
    service.execute_sql(owner, ready, GOOD, expected)  # nothing changed: runs
    shown = service.selections(owner, ready)
    service.put_selections(
        owner, ready, [{k: i[k] for k in ("kind", "schema", "object", "column", "mode")} for i in shown["items"]]
    )
    assert code(service.execute_sql, owner, ready, GOOD, expected) == "context_changed"  # the selection was saved again
    again = service.validate_sql(owner, ready, GOOD)["context"]
    expected = {"target": again["target"], "selection_revision": again["selection_revision"]}
    service.update(owner, ready, {"username": "somebody_else"})  # the credential would now go to another account
    assert code(service.execute_sql, owner, ready, GOOD, expected) == "context_changed"
    assert len(runs(driver)) == 1


def test_a_connection_pointed_at_another_database_after_validation_cannot_run_the_old_proposal(
    service, owner, ready, driver
):
    service.validate_sql(owner, ready, GOOD)
    service.update(owner, ready, {"database_name": "other_database"})
    assert code(service.execute_sql, owner, ready, GOOD) == "context_stale"
    assert runs(driver) == []


def test_a_disabled_connection_and_a_missing_key_run_nothing(service, owner, ready, driver, keys):
    keys.missing = True
    assert code(service.execute_sql, owner, ready, GOOD) in ("needs_credentials", "key_unavailable")
    keys.missing = False
    service.set_enabled(owner, ready, False)
    assert code(service.execute_sql, owner, ready, GOOD) == "connection_disabled" and runs(driver) == []


@pytest.mark.parametrize(
    "failure,expected",
    [
        (DriverError("timeout"), ("execution_timeout", 504, None)),
        (DriverError("cancelled"), ("execution_cancelled", 499, None)),
        (DriverError("account_can_write"), ("account_can_write", 409, None)),
        (DriverError("permission_denied"), ("permission_denied", 403, None)),
        (DriverError("query_error"), ("query_failed", 422, None)),
        (DriverError("unreachable"), ("execution_failed", 502, "unreachable")),
        (DriverError("auth_failed"), ("execution_failed", 502, "auth_failed")),
        (DriverError("tls_failed"), ("execution_failed", 502, "tls_failed")),
        (DriverError("failed"), ("execution_failed", 502, "failed")),
    ],
)
def test_every_failure_of_the_run_is_a_fixed_code_and_the_slot_and_the_connection_are_free_again(
    service, owner, ready, driver, events, failure, expected
):
    driver.run_error = failure
    error = pytest.raises(ConnectorError, service.execute_sql, owner, ready, GOOD).value
    assert (error.code, error.status, error.reason) == expected and error.__cause__ is None
    assert PASSWORD not in repr(vars(error))
    assert events.names()[-2][0] == "sql_execution_started" and events.names()[-1][0] == "sql_executed"
    driver.run_error = None
    assert service.execute_sql(owner, ready, GOOD)["status"] == "executed"  # nothing is stuck busy


# ---------- audit ----------


def test_a_security_event_is_written_before_the_run_and_one_after_and_neither_holds_the_sql(
    service, owner, ready, driver, events
):
    service.validate_sql(owner, ready, "SELECT 'secret_marker_zq' AS x")
    service.execute_sql(owner, ready, "SELECT 'secret_marker_zq' AS x")
    assert events.names()[-3:] == [("sql_validated", "ok"), ("sql_execution_started", "ok"), ("sql_executed", "ok")]
    assert all(item["principal_id"] == owner and item["event_ref"] == ready for item in events.items[-3:])
    assert "secret_marker_zq" not in json.dumps(events.items) and PASSWORD not in json.dumps(events.items)


def test_a_rejected_validation_and_a_cut_result_have_their_own_reasons(service, owner, ready, driver, events):
    service.validate_sql(owner, ready, "DROP TABLE x")
    assert events.names()[-1] == ("sql_validated", "rejected")
    driver.run_result = QueryResult([{"name": "id", "type": "int"}], [[1]], "rows", 0.1)
    service.execute_sql(owner, ready, GOOD)
    assert events.names()[-1] == ("sql_executed", "truncated")


def test_without_a_working_security_log_nothing_is_run(service, owner, ready, driver, events):
    events.lost = True
    error = pytest.raises(ConnectorError, service.execute_sql, owner, ready, GOOD).value
    assert (error.code, error.status) == ("security_log_unavailable", 503) and runs(driver) == []
    events.lost = False
    service.validate_sql(owner, ready, GOOD)  # (a successful write is what makes the log healthy again)
    assert service.execute_sql(owner, ready, GOOD)["status"] == "executed"
    events.lost = True  # a validation changes nothing: it does not need the log
    assert service.validate_sql(owner, ready, GOOD)["execution_allowed"]


def test_a_refused_attempt_to_execute_is_recorded_as_rejected_and_not_as_started(service, owner, ready, events):
    seen = len(events.items)
    with pytest.raises(ConnectorError):
        service.execute_sql(owner, ready, "DELETE FROM sales.customer")
    assert events.names()[seen:] == [("sql_validated", "rejected")]


def test_no_credential_and_no_sql_text_reach_the_log(service, owner, ready, driver, caplog):
    caplog.set_level("DEBUG")
    service.validate_sql(owner, ready, "SELECT 'secret_marker_zq' AS x FROM sales.customer")
    service.execute_sql(owner, ready, "SELECT 'secret_marker_zq' AS x FROM sales.customer")
    driver.run_error = DriverError("timeout")
    with pytest.raises(ConnectorError):
        service.execute_sql(owner, ready, "SELECT 'secret_marker_zq' AS x FROM sales.customer")
    for secret in ("secret_marker_zq", PASSWORD, "db.example.org", "SELECT"):
        assert secret not in caplog.text, secret
    assert "sql executed" in caplog.text and "sql_sha=" in caplog.text


# ---------- concurrency ----------


def test_executions_at_once_are_limited_and_one_connection_runs_one_query_at_a_time(
    keys, events, driver, owner, monkeypatch
):
    monkeypatch.setattr(config, "SQL_EXEC_MAX_CONCURRENT", 1)
    limited = Service(keys.vault, events, resolver=resolver(), internal=internal, driver_lookup=driver)
    ids = []
    for name in ("A", "B"):
        made = limited.create(owner, {**PAYLOAD, "name": name})
        limited.discover(owner, made["id"])
        limited.put_selections(owner, made["id"], [table("sales", "customer")])
        ids.append(made["id"])
    driver.run_gate = threading.Event()
    worker = threading.Thread(target=lambda: limited.execute_sql(owner, ids[0], "SELECT id FROM sales.customer"))
    worker.start()
    deadline = threading.Event()
    for _ in range(100):
        if runs(driver):
            break
        deadline.wait(0.05)
    assert runs(driver)
    same = pytest.raises(ConnectorError, limited.execute_sql, owner, ids[0], "SELECT id FROM sales.customer").value
    other = pytest.raises(ConnectorError, limited.execute_sql, owner, ids[1], "SELECT id FROM sales.customer").value
    assert (same.code, same.status) == ("busy", 409) and (other.code, other.status) == ("busy", 429)
    assert (
        code(limited.update, owner, ids[0], {"description": "x"}) == "busy"
    )  # and the connection cannot be changed meanwhile
    driver.run_gate.set()
    worker.join(10)
    driver.run_gate = None
    assert (
        limited.execute_sql(owner, ids[1], "SELECT id FROM sales.customer")["status"] == "executed"
    )  # slots are free again


def test_a_cancel_token_is_handed_to_the_driver(service, owner, ready, driver):
    token = CancelToken()
    service.execute_sql(owner, ready, GOOD, None, None, token)
    assert token._action is None  # attached while the run was going on, detached after it (the fake attaches one)


def test_cancel_token_fires_its_action_once_attached_even_if_cancelled_first():
    token, fired = CancelToken(), []
    token.cancel()
    token.attach(lambda: fired.append(1))
    assert fired == [1] and token.cancelled
    broken = CancelToken()
    broken.attach(lambda: 1 / 0)
    broken.cancel()  # an action that fails must not break the caller


# ---------- security audit: what changes while an execution is being prepared ----------


@pytest.mark.parametrize(
    "change,expected",
    [("disable", "connection_disabled"), ("other_account", "context_changed"), ("delete", "not_found")],
)
def test_a_connection_changed_while_its_execution_is_being_prepared_runs_nothing(
    keys, events, driver, owner, change, expected
):
    """The host name is resolved (up to seconds) after the connection was read and before it is claimed: a change that
    was answered "done" in that moment must hold for the run too."""
    hook = []
    answer = resolver()

    def slow_resolver(*args, **kwargs):
        while hook:
            hook.pop()()
        return answer(*args, **kwargs)

    service = Service(keys.vault, events, resolver=slow_resolver, internal=internal, driver_lookup=driver)
    cid = service.create(owner, PAYLOAD)["id"]
    service.discover(owner, cid)
    service.put_selections(owner, cid, [table("sales", "customer")])
    actions = {
        "disable": lambda: service.set_enabled(owner, cid, False),
        "other_account": lambda: service.update(owner, cid, {"username": "somebody_else"}),
        "delete": lambda: service.delete(owner, cid),
    }
    hook.append(actions[change])
    assert code(service.execute_sql, owner, cid, "SELECT id FROM sales.customer") == expected
    assert runs(driver) == [] and ("sql_execution_started", "ok") not in events.names()


def test_when_only_the_record_of_the_start_cannot_be_written_nothing_is_run(service, owner, ready, driver, events):
    write = events.emit

    def emit(event, reason, **who):
        return False if event == "sql_execution_started" else write(event, reason, **who)

    events.emit = emit  # (the log still says it is healthy: the write itself is what fails)
    error = pytest.raises(ConnectorError, service.execute_sql, owner, ready, GOOD).value
    assert (error.code, error.status) == ("security_log_unavailable", 503) and runs(driver) == []
    events.emit = write
    assert service.execute_sql(owner, ready, GOOD)["status"] == "executed"  # the slot and the connection were freed


def test_the_event_before_the_run_is_on_record_when_the_driver_is_called(service, owner, ready, driver, events):
    seen = []
    run = driver.run

    def watching(*args):
        seen.append(events.names()[-1])
        return run(*args)

    driver.run = watching
    service.execute_sql(owner, ready, GOOD)
    assert seen == [("sql_execution_started", "ok")]


def test_a_column_hidden_after_validation_is_hidden_from_the_run(service, owner, ready, driver):
    assert service.validate_sql(owner, ready, "SELECT name FROM sales.customer")["execution_allowed"]
    hidden = {"kind": "column", "schema": "sales", "object": "customer", "column": "name", "mode": "exclude"}
    service.put_selections(owner, ready, [table("sales", "customer"), hidden])
    error = pytest.raises(ConnectorError, service.execute_sql, owner, ready, "SELECT name FROM sales.customer").value
    assert (error.code, error.reason) == ("sql_rejected", "unknown_column")
    service.execute_sql(owner, ready, "SELECT * FROM sales.customer")
    assert '"name"' not in runs(driver)[-1][3] and len(runs(driver)) == 1


def test_a_host_changed_after_validation_is_refused_when_the_page_says_what_it_saw(service, owner, ready, driver):
    seen = service.validate_sql(owner, ready, GOOD)["context"]
    service.update(owner, ready, {"host": "other.example.org"})
    assert code(service.execute_sql, owner, ready, GOOD, {"target": seen["target"]}) == "context_changed"
    assert runs(driver) == []
