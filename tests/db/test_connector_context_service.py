"""M4.2 against a real PostgreSQL: selection, freshness, descriptions of changed schemas, and the schema context, all
through the service (fake drivers)."""

# ruff: noqa: F811  (the fixtures are imported from test_connector_service and then named as arguments)

import copy

import pytest

pytest.importorskip("cryptography")

from rag import config  # noqa: E402
from rag.connector.errors import ConnectorError  # noqa: E402
from tests.connector_support import PAYLOAD  # noqa: E402
from tests.db.test_connector_service import (  # noqa: E402, F401
    clean,
    driver,
    events,
    keys,
    owner,
    service,
    stranger,
)
from tests.db.test_connector_snapshots import CASES, SAMPLE, admin, code, discover  # noqa: E402, F401

pytestmark = pytest.mark.db


def table(s, o, mode="include"):
    return {"kind": "table", "schema": s, "object": o, "mode": mode}


def schema(s):
    return {"kind": "schema", "schema": s}


def column(s, o, c, mode="include"):
    return {"kind": "column", "schema": s, "object": o, "column": c, "mode": mode}


@pytest.fixture
def discovered(service, owner, driver):
    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    return made["id"]


def names(context):
    return {(s["name"], t["name"]) for s in context["context"]["schemas"] for t in s["objects"]}


# ---------- selection ----------


def test_a_new_connection_has_an_empty_selection_and_nothing_is_selected_by_default(service, owner, discovered):
    assert service.selections(owner, discovered) == {"revision": 0, "items": [], "unavailable": 0}
    service.discover(owner, discovered)
    assert service.selections(owner, discovered)["items"] == []


def test_a_selection_is_stored_with_its_revision_and_read_back_sorted(service, owner, discovered):
    stored = service.put_selections(owner, discovered, [table("sales", "customer"), schema("hr")])
    assert stored == {"revision": 1, "items": 2}
    shown = service.selections(owner, discovered)
    assert shown["revision"] == 1 and [
        (i["kind"], i["schema"], i["object"], i["available"]) for i in shown["items"]
    ] == [
        ("schema", "hr", "", True),
        ("table", "sales", "customer", True),
    ]
    assert service.put_selections(owner, discovered, [], base_revision=1) == {"revision": 2, "items": 0}


def test_a_stale_editor_gets_a_conflict_and_changes_nothing(service, owner, discovered):
    service.put_selections(owner, discovered, [schema("hr")])
    assert code(service.put_selections, owner, discovered, [schema("sales")], base_revision=0) == "selection_conflict"
    assert service.selections(owner, discovered)["items"][0]["schema"] == "hr"


def test_something_that_is_not_in_the_current_snapshot_cannot_be_newly_selected(service, owner, discovered):
    for items in ([table("sales", "nope")], [schema("nowhere")], [column("sales", "customer", "nope")]):
        assert code(service.put_selections, owner, discovered, items) == "object_not_found"
    assert service.selections(owner, discovered)["revision"] == 0


def test_an_invalid_combination_is_refused_with_its_code_and_stores_nothing(service, owner, discovered, events):
    assert (
        code(service.put_selections, owner, discovered, [table("sales", "customer", "exclude")]) == "invalid_selection"
    )
    assert code(service.put_selections, owner, discovered, [{"kind": "x"}]) == "invalid_selection"
    assert code(service.put_selections, owner, discovered, [schema("hr")], base_revision="1") == "invalid_request"
    assert (
        service.selections(owner, discovered)["revision"] == 0
        and ("schema_selection_changed", "ok") not in events.names()
    )


def test_selection_changes_are_recorded_in_the_security_log_before_they_happen(service, owner, discovered, events):
    service.put_selections(owner, discovered, [schema("hr")])
    assert events.names()[-1] == ("schema_selection_changed", "ok")
    events.lost = True
    with pytest.raises(ConnectorError):
        service.put_selections(owner, discovered, [schema("sales")])
    events.lost = False
    assert service.selections(owner, discovered)["items"][0]["schema"] == "hr"  # fail closed: nothing changed


def test_the_selection_survives_a_rediscovery_and_new_objects_are_not_selected_automatically(
    service, owner, discovered, driver
):
    service.put_selections(owner, discovered, [table("sales", "customer")])
    discover(service, owner, driver, discovered, CASES["table added"](SAMPLE))
    shown = service.selections(owner, discovered)
    assert [(i["object"], i["available"]) for i in shown["items"]] == [("customer", True)]
    assert names(service.context(owner, discovered, allow_stale=True)) == {("sales", "customer")}  # not "returns"


def test_an_item_whose_target_disappeared_is_kept_and_reported_unavailable_and_returns_when_the_target_does(
    service, owner, discovered, driver
):
    service.put_selections(owner, discovered, [table("sales", "order_note"), table("hr", "employee")])
    discover(service, owner, driver, discovered, CASES["table removed"](SAMPLE))
    shown = service.selections(owner, discovered)
    assert shown["unavailable"] == 1 and {(i["object"], i["available"]) for i in shown["items"]} == {
        ("order_note", False),
        ("employee", True),
    }
    # saving an unrelated edit does not drop the item whose target is gone
    service.put_selections(
        owner,
        discovered,
        shown["items"] and [{k: i[k] for k in ("kind", "schema", "object", "column", "mode")} for i in shown["items"]],
        base_revision=shown["revision"],
    )
    assert service.selections(owner, discovered)["unavailable"] == 1
    discover(service, owner, driver, discovered, SAMPLE)
    assert service.selections(owner, discovered)["unavailable"] == 0


def test_the_selection_of_one_owner_is_not_visible_or_writable_by_another(service, owner, stranger, discovered):
    service.put_selections(owner, discovered, [schema("hr")])
    assert code(service.selections, stranger, discovered) == "not_found"
    assert code(service.put_selections, stranger, discovered, []) == "not_found"
    assert code(service.context, stranger, discovered) == "not_found"
    assert code(service.acknowledge, stranger, discovered, 1) == "not_found"
    assert service.selections(owner, discovered)["items"][0]["schema"] == "hr"


def test_deleting_the_connection_takes_its_snapshots_changes_and_selection(service, owner, discovered, driver):
    service.put_selections(owner, discovered, [schema("hr")])
    discover(service, owner, driver, discovered, CASES["table added"](SAMPLE))
    service.delete(owner, discovered)
    with admin() as conn:
        for tablename in ("schema_snapshots", "schema_changes", "connection_selections"):
            assert (
                conn.execute(
                    f"SELECT count(*) AS n FROM {tablename} WHERE connection_id = %s", (discovered,)
                ).fetchone()["n"]
                == 0
            )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM connection_objects WHERE connection_id = %s", (discovered,)
            ).fetchone()["n"]
            == 0
        )


# ---------- freshness ----------


def test_the_states_follow_what_happens(service, owner, discovered, driver):
    assert service.freshness(owner, discovered)["state"] == "current"
    other = service.create(owner, {**PAYLOAD, "name": "Never"})
    assert service.freshness(owner, other["id"])["state"] == "never_discovered"
    # changes nobody selected anything of do not make it stale
    discover(service, owner, driver, discovered, CASES["table added"](SAMPLE))
    state = service.freshness(owner, discovered)
    assert state["state"] == "current" and state["unreviewed_changes"] == 1 and state["snapshot"]["version"] == 2


def test_a_change_to_a_selected_object_makes_the_context_stale_until_it_is_acknowledged(
    service, owner, discovered, driver
):
    service.put_selections(owner, discovered, [table("sales", "customer")])
    assert service.freshness(owner, discovered)["state"] == "current"
    discover(service, owner, driver, discovered, CASES["column added"](SAMPLE))
    state = service.freshness(owner, discovered)
    assert state["state"] == "stale" and state["reasons"] == ["unreviewed_changes"] and state["affected_selected"] == 1
    assert code(service.context, owner, discovered) == "context_stale"
    assert service.context(owner, discovered, allow_stale=True)["status"] == "stale"
    acknowledged = service.acknowledge(owner, discovered, 2)
    assert acknowledged["state"] == "current" and acknowledged["confirmed_version"] == 2
    assert service.context(owner, discovered)["usable"] is True


def test_acknowledging_an_old_version_is_refused_because_a_newer_snapshot_is_there(service, owner, discovered, driver):
    discover(service, owner, driver, discovered, CASES["table added"](SAMPLE))
    assert code(service.acknowledge, owner, discovered, 1) == "snapshot_changed"
    assert code(service.acknowledge, owner, discovered, "2") == "invalid_request"
    assert code(service.acknowledge, owner, discovered, True) == "invalid_request"


def test_saving_the_selection_confirms_it_against_the_current_snapshot(service, owner, discovered, driver):
    service.put_selections(owner, discovered, [table("sales", "customer")])
    discover(service, owner, driver, discovered, CASES["column added"](SAMPLE))
    assert service.freshness(owner, discovered)["state"] == "stale"
    shown = service.selections(owner, discovered)
    service.put_selections(
        owner,
        discovered,
        [{k: i[k] for k in ("kind", "schema", "object", "column", "mode")} for i in shown["items"]],
        base_revision=shown["revision"],
    )
    assert service.freshness(owner, discovered)["state"] == "current"


def test_a_removed_selected_object_makes_it_stale_and_acknowledging_does_not_hide_it(
    service, owner, discovered, driver
):
    service.put_selections(owner, discovered, [table("sales", "order_note")])
    discover(service, owner, driver, discovered, CASES["table removed"](SAMPLE))
    state = service.freshness(owner, discovered)
    assert state["state"] == "stale" and "removed_selected" in state["reasons"]
    service.acknowledge(owner, discovered, 2)
    assert "removed_selected" in service.freshness(owner, discovered)["reasons"]  # the item is still gone


def test_a_snapshot_not_confirmed_for_too_long_is_stale(service, owner, discovered, monkeypatch):
    monkeypatch.setattr(config, "CONNECTOR_SCHEMA_MAX_AGE_S", 60)
    with admin() as conn:
        conn.execute(
            "UPDATE schema_snapshots SET verified_at = now() - interval '2 hours' WHERE connection_id = %s",
            (discovered,),
        )
    state = service.freshness(owner, discovered)
    assert state["state"] == "stale" and state["reasons"] == ["age"]
    service.discover(owner, discovered)  # confirmed again, though nothing changed
    assert service.freshness(owner, discovered)["state"] == "current"


def test_a_change_to_a_column_that_a_partial_table_did_not_choose_is_not_a_reason_but_a_key_column_is(
    service, owner, discovered, driver
):
    service.put_selections(owner, discovered, [column("sales", "order_line", "customer_id")])
    discover(service, owner, driver, discovered, CASES["column added"](SAMPLE))  # customer, not selected at all
    assert service.freshness(owner, discovered)["state"] == "current"
    changed = copy.deepcopy(SAMPLE)
    changed["columns"] = [(*c[:4], "bigint", c[5]) if c[3] == "line_no" else c for c in changed["columns"]]
    discover(service, owner, driver, discovered, changed)  # line_no is part of order_line's primary key
    assert service.freshness(owner, discovered)["state"] == "stale"


# ---------- descriptions after a change ----------


def test_a_description_of_something_that_vanished_is_kept_as_orphaned_and_returns_with_it(
    service, owner, discovered, driver
):
    service.describe(owner, discovered, "table", "sales", "order_note", "", "notes about orders")
    discover(service, owner, driver, discovered, CASES["table removed"](SAMPLE))
    kept = [d for d in service.descriptions(owner, discovered) if d["object_name"] == "order_note"]
    assert kept and kept[0]["orphaned"] is True and kept[0]["description"] == "notes about orders"
    assert service.freshness(owner, discovered)["orphaned_descriptions"] == 1
    discover(service, owner, driver, discovered, SAMPLE)
    assert [d["orphaned"] for d in service.descriptions(owner, discovered) if d["object_name"] == "order_note"] == [
        False
    ]
    assert service.freshness(owner, discovered)["orphaned_descriptions"] == 0


def test_a_description_may_not_hold_a_secret(service, owner, discovered):
    from tests.connector_support import PASSWORD

    assert (
        code(service.describe, owner, discovered, "database", "", "", "", f"the password is {PASSWORD}")
        == "description_contains_secret"
    )
    assert (
        code(service.describe, owner, discovered, "database", "", "", "", "password = hunter2hunter2")
        == "description_contains_secret"
    )
    assert (
        code(
            service.describe,
            owner,
            discovered,
            "database",
            "",
            "",
            "",
            "see postgresql://reader:hunter2@db.example.org/sales",
        )
        == "description_contains_secret"
    )
    assert (
        service.describe(owner, discovered, "database", "", "", "", "the sales database")["description"]
        == "the sales database"
    )


# ---------- the context ----------


def test_the_context_has_only_what_is_selected_with_descriptions_and_no_credentials(service, owner, discovered):
    service.describe(owner, discovered, "table", "sales", "customer", "", "who buys")
    service.describe(owner, discovered, "column", "sales", "customer", "name", "full name")
    service.put_selections(owner, discovered, [table("sales", "customer"), table("sales", "order_line")])
    result = service.context(owner, discovered)
    assert result["usable"] is True and result["status"] == "ok" and result["freshness"]["state"] == "current"
    assert names(result) == {("sales", "customer"), ("sales", "order_line")}
    text = str(result)
    assert "full name" in text and "who buys" in text and "reader" not in text and "db.example.org" not in text
    assert "hr" not in {s["name"] for s in result["context"]["schemas"]}
    assert result["snapshot"]["version"] == 1 and len(result["snapshot"]["fingerprint"]) == 64


def test_the_same_snapshot_and_selection_give_the_same_context_byte_for_byte(service, owner, discovered):
    service.put_selections(owner, discovered, [schema("sales"), schema("hr")])
    first = service.context(owner, discovered)
    service.discover(owner, discovered)  # nothing changed
    second = service.context(owner, discovered)
    assert (
        first["budget"]["context_fingerprint"] == second["budget"]["context_fingerprint"]
        and first["context"] == second["context"]
    )


def test_the_context_respects_the_size_budget_and_says_what_it_left_out(service, owner, discovered):
    service.put_selections(owner, discovered, [schema("sales"), schema("hr")])
    full = service.context(owner, discovered)
    small = service.context(owner, discovered, max_chars=600)
    assert full["complete"] is True and small["complete"] is False
    assert small["budget"]["used_chars"] <= 600 and small["context"]["omitted"]["reason"] == "budget"
    assert code(service.context, owner, discovered, max_chars=10) == "invalid_request"


def test_an_empty_selection_gives_an_empty_context_that_is_not_usable(service, owner, discovered):
    result = service.context(owner, discovered)
    assert result["usable"] is False and names(result) == set() and result["selection"]["selected_objects"] == 0


def test_a_stale_context_is_refused_unless_it_is_asked_for_as_is_and_then_it_says_so(
    service, owner, discovered, driver
):
    service.put_selections(owner, discovered, [table("sales", "customer")])
    discover(service, owner, driver, discovered, CASES["column removed"](SAMPLE))
    refused = None
    try:
        service.context(owner, discovered)
    except Exception as error:  # noqa: BLE001
        refused = error
    assert (
        refused is not None and refused.code == "context_stale" and refused.status == 409 and refused.reason == "stale"
    )
    shown = service.context(owner, discovered, allow_stale=True)
    assert shown["usable"] is False and shown["status"] == "stale" and shown["freshness"]["state"] == "stale"


def test_a_failed_discovery_makes_the_context_failed_but_it_can_still_be_looked_at(service, owner, discovered, driver):
    service.put_selections(owner, discovered, [table("sales", "customer")])
    driver.fail_after, driver.discover_error = 1, "timeout"
    with pytest.raises(Exception):  # noqa: B017
        service.discover(owner, discovered)
    assert code(service.context, owner, discovered) == "context_stale"
    shown = service.context(owner, discovered, allow_stale=True)
    assert shown["status"] == "failed" and shown["usable"] is False and names(shown) == {("sales", "customer")}


def test_a_disabled_connection_gives_no_context(service, owner, discovered):
    service.set_enabled(owner, discovered, False)
    assert code(service.context, owner, discovered, allow_stale=True) == "connection_disabled"


def test_a_connection_that_was_never_discovered_gives_a_context_that_says_so(service, owner):
    made = service.create(owner, PAYLOAD)
    assert code(service.context, owner, made["id"]) == "context_stale"
    shown = service.context(owner, made["id"], allow_stale=True)
    assert shown["status"] == "never_discovered" and shown["usable"] is False and shown["snapshot"] is None


def test_a_column_exclusion_removes_the_column_from_the_context(service, owner, discovered):
    service.put_selections(
        owner, discovered, [table("sales", "customer"), column("sales", "customer", "name", "exclude")]
    )
    result = service.context(owner, discovered)
    customer = next(t for s in result["context"]["schemas"] for t in s["objects"] if t["name"] == "customer")
    assert [c["name"] for c in customer["columns"]] == ["id"]


def test_hostile_names_are_only_data_in_selections_changes_descriptions_and_the_context(service, owner, driver):
    nasty = "x'; DROP TABLE connections; --"
    odd = {
        "objects": [("s'\"; --", nasty, "table")],
        "columns": [("s'\"; --", nasty, 1, "c'; DELETE FROM connections; --", "text", True)],
        "constraints": [],
        "indexes": [],
    }
    made = service.create(owner, PAYLOAD)
    driver.sample = odd
    service.discover(owner, made["id"])
    service.put_selections(owner, made["id"], [table("s'\"; --", nasty)])
    service.describe(owner, made["id"], "table", "s'\"; --", nasty, "", "a description")
    driver.sample = {**odd, "columns": odd["columns"] + [("s'\"; --", nasty, 2, "$1; --", "text", True)]}
    service.discover(owner, made["id"])
    listed = service.changes(owner, made["id"], object_type="column")["changes"]
    assert [c["sub_name"] for c in listed] == ["$1; --"]
    result = service.context(owner, made["id"], allow_stale=True)
    assert names(result) == {("s'\"; --", nasty)}
    assert service.get(owner, made["id"])["name"] == "Sales"  # the connections table is still there


def test_two_saves_of_the_selection_at_once_with_the_same_revision_let_exactly_one_win(service, owner, discovered):
    import threading

    outcomes, barrier = [], threading.Barrier(2)

    def save(item):
        barrier.wait()
        try:
            outcomes.append(service.put_selections(owner, discovered, [item], base_revision=0)["revision"])
        except ConnectorError as error:
            outcomes.append(error.code)

    threads = [threading.Thread(target=save, args=(i,)) for i in (schema("hr"), schema("sales"))]
    [t.start() for t in threads]
    [t.join(20) for t in threads]
    assert sorted(map(str, outcomes)) == ["1", "selection_conflict"]
    assert (
        service.selections(owner, discovered)["revision"] == 1
        and len(service.selections(owner, discovered)["items"]) == 1
    )
