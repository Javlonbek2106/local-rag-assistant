"""M4.2 against a real PostgreSQL: snapshots, fingerprints, change detection, failure and concurrency behaviour,
retention, and the upgrade of the rows M4.1b made. Fake drivers (tests/connector_support.py); the real ones are in the
integration tests."""

# ruff: noqa: F811  (the fixtures are imported from test_connector_service and then named as arguments)

import copy
import os
import random
import threading
import uuid

import psycopg
import pytest

pytest.importorskip("cryptography")

from rag import config, db  # noqa: E402
from rag.connector import repository, schema_model  # noqa: E402
from rag.connector import snapshots as snap  # noqa: E402
from rag.connector.errors import ConnectorError, DriverError  # noqa: E402
from rag.connector.service import Service  # noqa: E402
from tests.connector_support import PAYLOAD, internal, resolver  # noqa: E402
from tests.connector_support import SAMPLE as _SAMPLE  # noqa: E402
from tests.db.test_connector_service import (  # noqa: E402, F401
    admin,
    clean,
    code,
    driver,
    events,
    keys,
    owner,
    service,
    stranger,
)

pytestmark = pytest.mark.db


def _padded(rows):
    return [tuple(r) + (None,) * (8 - len(r)) for r in rows]


SAMPLE = {**copy.deepcopy(_SAMPLE), "constraints": _padded(_SAMPLE["constraints"])}


def sample(_base=None, **changes):
    out = copy.deepcopy(_base or SAMPLE)
    out.update(changes)
    return out


def snapshot_count(connection_id):
    with admin() as conn:
        return conn.execute(
            "SELECT count(*) AS n FROM schema_snapshots WHERE connection_id = %s", (connection_id,)
        ).fetchone()["n"]


def rows_per_snapshot(connection_id):
    with admin() as conn:
        rows = conn.execute(
            "SELECT s.version, count(o.id) AS n FROM schema_snapshots s LEFT JOIN connection_objects o ON "
            "o.snapshot_id = s.id "
            "WHERE s.connection_id = %s GROUP BY s.version ORDER BY s.version",
            (connection_id,),
        ).fetchall()
    return {r["version"]: r["n"] for r in rows}


def discover(service, owner, driver, connection_id, new_sample=None):
    if new_sample is not None:
        driver.sample = new_sample
    return service.discover(owner, connection_id)["snapshot"]


# ---------- an oracle: the same rules written the plain way, to check the SQL against ----------


def oracle(old, new):
    """The set of (object_type, schema, object, sub, change) that two samples differ by."""

    def objects(s):
        return {(o[0], o[1]): o[2] for o in s["objects"]}

    def kind(t):
        return "table" if t == "table" else "view"

    a, b = objects(old), objects(new)
    out = set()
    for schema in {k[0] for k in a} ^ {k[0] for k in b}:
        out.add(("schema", schema, "", "", "added" if schema in {k[0] for k in b} else "removed"))
    for key in set(a) | set(b):
        if key not in a:
            out.add((kind(b[key]), *key, "", "added"))
        elif key not in b:
            out.add((kind(a[key]), *key, "", "removed"))
        elif a[key] != b[key]:
            out.add((kind(b[key]), *key, "", "modified"))
    both = set(a) & set(b)

    def columns(s):
        return {(c[0], c[1], c[3]): (c[4], bool(c[5])) for c in s["columns"] if (c[0], c[1]) in both}

    ca, cb = columns(old), columns(new)
    for key in set(ca) | set(cb):
        if key not in ca:
            out.add(("column", key[0], key[1], key[2], "added"))
        elif key not in cb:
            out.add(("column", key[0], key[1], key[2], "removed"))
        elif ca[key] != cb[key]:
            out.add(("column", key[0], key[1], key[2], "modified"))

    def constraints(s):
        found = {}
        for c in s["constraints"]:
            if (c[0], c[1]) in both:
                ident = "" if c[2] == "primary_key" else c[3]
                found[(c[0], c[1], c[2], ident)] = (c[3], list(c[4]), c[5], c[6], list(c[7]) if c[7] else None)
        return found

    ka, kb = constraints(old), constraints(new)
    for key in set(ka) | set(kb):
        label = "relationship" if key[2] == "foreign_key" else "constraint"
        name = (kb.get(key) or ka.get(key))[0]
        if key not in ka:
            out.add((label, key[0], key[1], name, "added"))
        elif key not in kb:
            out.add((label, key[0], key[1], name, "removed"))
        elif ka[key][1:] != kb[key][1:]:
            out.add((label, key[0], key[1], name, "modified"))

    def indexes(s):
        found = {}
        for i in s["indexes"]:
            if (i[0], i[1]) in both:
                found[(i[0], i[1], "" if i[5] else i[2])] = (i[2], list(i[3]), bool(i[4]), bool(i[5]))
        return found

    ia, ib = indexes(old), indexes(new)
    for key in set(ia) | set(ib):
        name = (ib.get(key) or ia.get(key))[0]
        if key not in ia:
            out.add(("index", key[0], key[1], name, "added"))
        elif key not in ib:
            out.add(("index", key[0], key[1], name, "removed"))
        elif ia[key][1:] != ib[key][1:]:
            out.add(("index", key[0], key[1], name, "modified"))
    return out


def recorded(service, owner, connection_id, version):
    listed = service.changes(owner, connection_id, version=version, limit=500)
    return {
        (c["object_type"], c["schema_name"], c["object_name"], c["sub_name"], c["change_type"])
        for c in listed["changes"]
    }, listed


# ---------- the first discovery, and the one that finds nothing new ----------


def test_the_first_discovery_is_snapshot_1_with_the_fingerprint_the_plain_algorithm_gives(service, owner, driver):
    made = service.create(owner, PAYLOAD)
    result = service.discover(owner, made["id"])
    assert (
        result["snapshot"]["version"] == 1
        and result["snapshot"]["first"] is True
        and result["snapshot"]["changes"] == 0
    )
    expected = schema_model.fingerprint_of(
        "postgresql",
        schema_model.records_of(SAMPLE["objects"], SAMPLE["columns"], SAMPLE["constraints"], SAMPLE["indexes"]),
    )
    assert result["snapshot"]["fingerprint"] == expected  # streamed in SQL order == sorted in Python by bytes
    shown = service.snapshots(owner, made["id"])
    assert [(s["version"], s["is_current"], s["objects_count"], s["columns_count"]) for s in shown] == [
        (1, True, 6, 11)
    ]
    assert (
        shown[0]["constraints_count"] == 6 and shown[0]["indexes_count"] == 2 and shown[0]["provider"] == "postgresql"
    )
    assert shown[0]["database_name"] == "sales" and shown[0]["fingerprint_version"] == schema_model.FINGERPRINT_VERSION
    assert service.changes(owner, made["id"])["total"] == 0  # a first snapshot has nothing before it to differ from


def test_a_second_discovery_that_finds_the_same_makes_no_new_snapshot_and_duplicates_nothing(service, owner, driver):
    made = service.create(owner, PAYLOAD)
    first = service.discover(owner, made["id"])
    before = service.snapshots(owner, made["id"])[0]
    again = service.discover(owner, made["id"])
    assert again["snapshot"]["changed"] is False and again["snapshot"]["version"] == 1
    assert again["snapshot"]["fingerprint"] == first["snapshot"]["fingerprint"]
    after = service.snapshots(owner, made["id"])
    assert (
        len(after) == 1
        and after[0]["verified_at"] > before["verified_at"]
        and after[0]["created_at"] == before["created_at"]
    )
    assert rows_per_snapshot(made["id"]) == {1: 6}  # the rows written by the second run were rolled back
    assert service.objects(owner, made["id"])[1] == 6


def test_the_order_the_server_lists_things_in_does_not_make_a_new_snapshot(service, owner, driver):
    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    rng = random.Random(3)
    for _ in range(5):
        shuffled = sample()
        for group in shuffled.values():
            rng.shuffle(group)
        assert discover(service, owner, driver, made["id"], shuffled)["changed"] is False
    assert snapshot_count(made["id"]) == 1


def test_the_name_of_a_primary_key_and_the_position_of_columns_are_not_a_change(service, owner, driver):
    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    renamed = sample()
    renamed["constraints"] = [
        (c[0], c[1], c[2], "PK__generated__" + uuid.uuid4().hex[:8] if c[2] == "primary_key" else c[3], *c[4:])
        for c in renamed["constraints"]
    ]
    renamed["columns"] = [(c[0], c[1], 99 - c[2], *c[3:]) for c in renamed["columns"]]
    assert discover(service, owner, driver, made["id"], renamed)["changed"] is False


# ---------- every kind of change is found, as the plain rules say ----------


def rename_customer(s):
    def table(c):
        return "buyer" if c == "customer" else c

    return sample(
        s,
        objects=[(o[0], table(o[1]), o[2]) for o in s["objects"]],
        columns=[(c[0], table(c[1]), *c[2:]) for c in s["columns"]],
        constraints=[
            (c[0], table(c[1]), c[2], c[3], c[4], c[5], table(c[6]) if c[6] else c[6], c[7]) for c in s["constraints"]
        ],
    )


CASES = {
    "table added": lambda s: sample(
        s,
        objects=s["objects"] + [("sales", "returns", "table")],
        columns=s["columns"] + [("sales", "returns", 1, "id", "integer", False)],
    ),
    "table removed": lambda s: sample(
        s,
        objects=[o for o in s["objects"] if o[1] != "order_note"],
        columns=[c for c in s["columns"] if c[1] != "order_note"],
        constraints=[c for c in s["constraints"] if c[1] != "order_note"],
    ),
    "view added": lambda s: sample(
        s,
        objects=s["objects"] + [("sales", "vip", "view")],
        columns=s["columns"] + [("sales", "vip", 1, "id", "integer", True)],
    ),
    "schema added": lambda s: sample(
        s,
        objects=s["objects"] + [("fin", "ledger", "table")],
        columns=s["columns"] + [("fin", "ledger", 1, "id", "integer", False)],
    ),
    "schema removed": lambda s: sample(
        s,
        objects=[o for o in s["objects"] if o[0] != "hr"],
        columns=[c for c in s["columns"] if c[0] != "hr"],
        constraints=[c for c in s["constraints"] if c[0] != "hr"],
        indexes=[i for i in s["indexes"] if i[0] != "hr"],
    ),
    "column added": lambda s: sample(s, columns=s["columns"] + [("sales", "customer", 3, "email", "text", True)]),
    "column removed": lambda s: sample(s, columns=[c for c in s["columns"] if c[3] != "name"]),
    "column type changed": lambda s: sample(
        s, columns=[(*c[:4], "varchar(50)", c[5]) if c[3] == "name" else c for c in s["columns"]]
    ),
    "column nullability changed": lambda s: sample(
        s, columns=[(*c[:5], True) if c[3] == "name" else c for c in s["columns"]]
    ),
    "primary key changed": lambda s: sample(
        s, constraints=[(*c[:4], ["order_id"], *c[5:]) if c[3] == "pk_ol" else c for c in s["constraints"]]
    ),
    "foreign key columns changed": lambda s: sample(
        s, constraints=[(*c[:4], ["order_id"], *c[5:]) if c[3] == "fk_ol_customer" else c for c in s["constraints"]]
    ),
    "foreign key target changed": lambda s: sample(
        s, constraints=[(*c[:5], "hr", "employee", ["id"]) if c[3] == "fk_ol_customer" else c for c in s["constraints"]]
    ),
    "foreign key added": lambda s: sample(
        s,
        constraints=s["constraints"]
        + [("sales", "order_note", "foreign_key", "fk_extra", ["order_id"], "sales", "customer", ["id"])],
    ),
    "foreign key removed": lambda s: sample(s, constraints=[c for c in s["constraints"] if c[3] != "fk_ol_customer"]),
    "unique added": lambda s: sample(
        s, constraints=s["constraints"] + [("sales", "customer", "unique", "uq_name", ["name"], None, None, None)]
    ),
    "unique changed": lambda s: sample(
        s,
        constraints=[
            (c[0], c[1], c[2], c[3], ["email", "id"], *c[5:]) if c[3] == "uq_email" else c for c in s["constraints"]
        ],
    ),
    "index added": lambda s: sample(
        s, indexes=s["indexes"] + [("sales", "customer", "ix_name", ["name"], False, False)]
    ),
    "index changed": lambda s: sample(
        s, indexes=[(*i[:3], ["email", "id"], *i[4:]) if i[2] == "ix_email" else i for i in s["indexes"]]
    ),
    "index removed": lambda s: sample(s, indexes=[i for i in s["indexes"] if i[2] != "ix_email"]),
    "a table became a view": lambda s: sample(
        s, objects=[(o[0], o[1], "view") if o[1] == "no_pk" else o for o in s["objects"]]
    ),
    "a rename is a removal and an addition": lambda s: rename_customer(s),
}


def combined(*names):
    """Several of the CASES at once: each one applied to the result of the one before."""
    out = SAMPLE
    for name in names:
        out = CASES[name](out)
    return out


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_change_is_recorded_exactly_as_the_plain_rules_say_and_makes_snapshot_2(service, owner, driver, case):
    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    new = CASES[case](SAMPLE)
    result = discover(service, owner, driver, made["id"], new)
    assert result["changed"] is True and result["version"] == 2 and result["changes"] > 0
    found, listed = recorded(service, owner, made["id"], 2)
    assert found == oracle(SAMPLE, new), case
    assert listed["total"] == len(found) == result["changes"]
    assert snapshot_count(made["id"]) == 2


def test_a_rename_is_never_called_a_rename(service, owner, driver):
    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    discover(service, owner, driver, made["id"], CASES["a rename is a removal and an addition"](SAMPLE))
    kinds = {
        (c["object_type"], c["object_name"], c["change_type"])
        for c in service.changes(owner, made["id"], limit=500)["changes"]
    }
    assert ("table", "customer", "removed") in kinds and ("table", "buyer", "added") in kinds
    assert not [c for c in service.changes(owner, made["id"], limit=500)["changes"] if c["change_type"] == "renamed"]


def test_the_previous_and_current_definitions_are_kept_with_each_change(service, owner, driver):
    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    discover(service, owner, driver, made["id"], CASES["column type changed"](SAMPLE))
    listed = service.changes(owner, made["id"], object_type="column")["changes"]
    assert len(listed) == 1
    change = listed[0]
    assert (change["schema_name"], change["object_name"], change["sub_name"], change["change_type"]) == (
        "sales",
        "customer",
        "name",
        "modified",
    )
    assert change["previous"] == {"data_type": "text", "nullable": False} and change["current"] == {
        "data_type": "varchar(50)",
        "nullable": False,
    }
    assert change["to_version"] == 2 and change["detected_at"] is not None
    discover(service, owner, driver, made["id"], CASES["foreign key target changed"](SAMPLE))
    fk = service.changes(owner, made["id"], object_type="relationship", version=3)["changes"][0]
    assert (
        fk["previous"]["ref_object"] == "customer"
        and fk["current"]["ref_schema"] == "hr"
        and fk["current"]["ref_columns"] == ["id"]
    )
    added = service.changes(owner, made["id"], object_type="table", change_type="removed")
    assert added["total"] == 0 and added["summary"] == {}


def test_a_whole_table_added_is_one_change_not_one_per_column(service, owner, driver):
    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    wide = sample(
        objects=SAMPLE["objects"] + [("sales", "wide", "table")],
        columns=SAMPLE["columns"] + [("sales", "wide", n, f"c{n}", "integer", True) for n in range(1, 40)],
        constraints=SAMPLE["constraints"] + [("sales", "wide", "primary_key", "pk", ["c1"], None, None, None)],
    )
    discover(service, owner, driver, made["id"], wide)
    changes = service.changes(owner, made["id"], limit=500)
    assert changes["total"] == 1 and changes["changes"][0]["current"] == {"type": "table", "columns": 39}


def test_changes_can_be_filtered_and_are_counted_by_kind(service, owner, driver):
    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    new = combined("table removed", "column added")
    discover(service, owner, driver, made["id"], new)
    everything = service.changes(owner, made["id"], limit=500)
    assert everything["summary"] == {"added": 1, "removed": 1} and everything["total"] == 2
    assert service.changes(owner, made["id"], change_type="added")["total"] == 1
    assert service.changes(owner, made["id"], object_type="table")["total"] == 1
    assert service.changes(owner, made["id"], since_version=2)["total"] == 0
    assert (
        service.changes(owner, made["id"], limit=1, offset=1)["changes"]
        != service.changes(owner, made["id"], limit=1)["changes"]
    )
    for bad in ({"object_type": "planet"}, {"change_type": "renamed"}):
        assert code(service.changes, owner, made["id"], **bad) == "invalid_request"


def test_the_changes_come_back_in_a_fixed_order(service, owner, driver):
    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    discover(service, owner, driver, made["id"], combined("schema added", "column added", "table removed"))
    first = service.changes(owner, made["id"], limit=500)["changes"]
    again = service.changes(owner, made["id"], limit=500)["changes"]
    assert first == again
    order = [c["object_type"] for c in first]
    assert order == sorted(
        order, key=["schema", "table", "view", "relationship", "constraint", "column", "index"].index
    )


# ---------- a failed or cut-off discovery ----------


def test_a_failed_discovery_keeps_the_last_good_snapshot_and_is_remembered_apart(service, owner, driver, events):
    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    good = service.snapshots(owner, made["id"])
    driver.sample = CASES["table added"](SAMPLE)
    driver.fail_after, driver.discover_error = 3, "timeout"
    with pytest.raises(ConnectorError) as failed:
        service.discover(owner, made["id"])
    assert failed.value.reason == "timeout"
    assert service.snapshots(owner, made["id"]) == good  # the same snapshots, the same current one
    assert service.objects(owner, made["id"])[1] == 6
    shown = service.get(owner, made["id"])
    assert shown["last_discovery_status"] == "failed" and shown["last_discovery_error"] == "timeout"
    assert shown["last_discovery_at"] is not None and shown["schema_discovered_at"] is not None
    assert service.freshness(owner, made["id"])["state"] == "failed"
    assert events.names()[-1] == ("schema_discovered", "failed")
    driver.fail_after, driver.discover_error = None, None
    service.discover(owner, made["id"])
    assert (
        service.freshness(owner, made["id"])["state"] == "current"
        and service.get(owner, made["id"])["last_discovery_error"] is None
    )


def test_a_partial_discovery_leaves_no_trace_and_is_never_a_snapshot(service, owner, driver):
    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    driver.sample = CASES["table added"](SAMPLE)
    for fail_after in (0, 1, 3, 5):
        driver.fail_after, driver.discover_error = fail_after, "unreachable"
        with pytest.raises(ConnectorError):
            service.discover(owner, made["id"])
    assert snapshot_count(made["id"]) == 1 and rows_per_snapshot(made["id"]) == {1: 6}
    with admin() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM connection_objects WHERE connection_id = %s", (made["id"],)
            ).fetchone()["n"]
            == 6
        )
        assert (
            conn.execute("SELECT count(*) AS n FROM schema_changes WHERE connection_id = %s", (made["id"],)).fetchone()[
                "n"
            ]
            == 0
        )


def test_a_failed_first_discovery_leaves_nothing_and_is_not_never_discovered(service, owner, driver):
    made = service.create(owner, PAYLOAD)
    assert service.freshness(owner, made["id"])["state"] == "never_discovered"
    driver.fail_after, driver.discover_error = 2, "auth_failed"
    with pytest.raises(ConnectorError):
        service.discover(owner, made["id"])
    state = service.freshness(owner, made["id"])
    assert state["state"] == "failed" and state["has_snapshot"] is False and state["last_error"] == "auth_failed"
    assert service.snapshots(owner, made["id"]) == [] and service.objects(owner, made["id"])[1] == 0


def test_a_discovery_that_is_too_large_is_a_failure_and_keeps_the_snapshot(service, owner, driver, monkeypatch):
    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    monkeypatch.setattr(config, "CONNECTOR_MAX_OBJECTS", 2)
    driver.sample = CASES["table added"](SAMPLE)
    with pytest.raises(ConnectorError) as failed:
        service.discover(owner, made["id"])
    assert failed.value.reason == "too_large" and snapshot_count(made["id"]) == 1


def test_a_discovery_that_began_and_never_ended_stops_counting_after_its_lease(service, owner, driver, monkeypatch):
    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    snap.begin_discovery(owner, made["id"])  # started ... and the process died
    assert service.freshness(owner, made["id"])["state"] == "in_progress"
    with admin() as conn:
        conn.execute(
            "UPDATE connections SET discovery_started_at = now() - interval '1 hour' WHERE id = %s", (made["id"],)
        )
    state = service.freshness(owner, made["id"])
    assert state["state"] == "current" and state["in_progress"] is False


# ---------- versions, retention, limits ----------


def test_versions_count_up_and_are_never_reused_and_old_ones_are_kept_only_up_to_the_retention(
    service, owner, driver, monkeypatch
):
    monkeypatch.setattr(config, "CONNECTOR_SNAPSHOT_RETENTION", 3)
    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    for number in range(2, 8):
        extra = [("sales", f"t{number}", "table")]
        discover(
            service,
            owner,
            driver,
            made["id"],
            sample(
                objects=SAMPLE["objects"] + extra,
                columns=SAMPLE["columns"] + [("sales", f"t{number}", 1, "id", "integer", False)],
            ),
        )
    kept = service.snapshots(owner, made["id"])
    assert [s["version"] for s in kept] == [7, 6, 5]  # the newest three (the retention); the current one is never out
    assert kept[0]["is_current"]
    assert max(rows_per_snapshot(made["id"])) == 7 and len(rows_per_snapshot(made["id"])) == len(kept)
    with admin() as conn:
        left = {
            r["to_version"]
            for r in conn.execute(
                "SELECT DISTINCT to_version FROM schema_changes WHERE connection_id = %s", (made["id"],)
            )
        }
    assert left <= {s["version"] for s in kept}  # the changes that led to a removed snapshot went with it
    assert service.snapshot(owner, made["id"], kept[0]["id"])["version"] == 7


def test_more_changes_than_the_limit_are_cut_object_level_first_and_the_cut_is_flagged(
    service, owner, driver, monkeypatch
):
    monkeypatch.setattr(config, "CONNECTOR_MAX_CHANGES", 4)
    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    new = sample(
        objects=SAMPLE["objects"] + [("sales", f"n{i}", "table") for i in range(3)],
        columns=SAMPLE["columns"] + [("sales", "customer", 10 + i, f"extra{i}", "text", True) for i in range(6)],
    )
    result = discover(service, owner, driver, made["id"], new)
    assert result["changes"] == 9 and result["changes_truncated"] is True
    kept = service.changes(owner, made["id"], limit=500)["changes"]
    assert (
        len(kept) == 4 and [c["object_type"] for c in kept].count("table") == 3
    )  # the tables stay, the columns are cut
    assert service.snapshots(owner, made["id"])[0]["changes_truncated"] is True


def test_the_snapshots_of_one_owner_are_invisible_to_another(service, owner, stranger, driver):
    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    snapshot = service.snapshots(owner, made["id"])[0]
    for attempt in (
        lambda: service.snapshots(stranger, made["id"]),
        lambda: service.snapshot(stranger, made["id"], snapshot["id"]),
        lambda: service.changes(stranger, made["id"]),
        lambda: service.freshness(stranger, made["id"]),
    ):
        assert code(attempt) == "not_found"
    assert (
        snap.list_snapshots(stranger, made["id"]) == []
        and snap.get_snapshot(stranger, made["id"], snapshot["id"]) is None
    )
    assert snap.list_changes(stranger, made["id"])[1] == 0 and snap.facts(stranger, made["id"]) is None


def test_a_snapshot_by_id_has_its_schemas_and_an_unknown_one_is_not_found(service, owner, driver):
    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    first = service.snapshots(owner, made["id"])[0]
    discover(service, owner, driver, made["id"], CASES["schema removed"](SAMPLE))
    old = service.snapshot(owner, made["id"], first["id"])
    assert {(s["schema_name"], s["tables"], s["views"]) for s in old["schemas"]} == {("hr", 2, 0), ("sales", 3, 1)}
    assert old["is_current"] is False and old["version"] == 1
    for bad in ("not-an-id", str(uuid.uuid4())):
        assert code(service.snapshot, owner, made["id"], bad) == "snapshot_not_found"


def test_the_browsing_of_the_schema_shows_the_current_snapshot_only(service, owner, driver):
    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    discover(service, owner, driver, made["id"], CASES["table removed"](SAMPLE))
    assert service.objects(owner, made["id"])[1] == 5 and {
        s["schema_name"] for s in service.schemas(owner, made["id"])
    } == {"hr", "sales"}
    assert code(service.object_detail, owner, made["id"], "sales", "order_note") == "object_not_found"
    assert {(r["object_name"]) for r in service.relationships(owner, made["id"])} == {"order_line"}


# ---------- two discoveries at once ----------


def test_two_discoveries_of_one_connection_at_the_database_level_are_serialised(service, owner, driver):
    made = service.create(owner, PAYLOAD)
    results, errors = [], []

    def run(new):
        try:
            with repository.schema_replacement(owner, made["id"], 1000, 10000) as writer:
                for row in new["objects"]:
                    writer.object(*row)
                for row in new["columns"]:
                    writer.column(*row)
            results.append(writer.result)
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    variants = [SAMPLE, CASES["table added"](SAMPLE), CASES["column added"](SAMPLE), CASES["table removed"](SAMPLE)]
    threads = [threading.Thread(target=run, args=(v,)) for v in variants]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert not errors and len(results) == 4
    versions = sorted(r["version"] for r in results if r["changed"] or r["first"])
    assert versions == sorted(set(versions))  # no version twice
    current = {s["version"] for s in service.snapshots(owner, made["id"]) if s["is_current"]}
    assert len(current) == 1 and max(versions) in current
    with admin() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM connections WHERE id = %s AND current_snapshot_id IS NOT NULL", (made["id"],)
            ).fetchone()["n"]
            == 1
        )


def test_a_second_discovery_of_the_same_connection_through_the_service_is_refused_while_one_runs(
    service, owner, driver
):
    made = service.create(owner, PAYLOAD)
    inside, release = threading.Event(), threading.Event()
    original = driver.introspect

    def slow(target, password, sink, timeout):
        inside.set()
        release.wait(5)
        return original(target, password, sink, timeout)

    driver.introspect = slow
    worker = threading.Thread(target=lambda: service.discover(owner, made["id"]))
    worker.start()
    assert inside.wait(5)
    assert service.freshness(owner, made["id"])["state"] == "in_progress"
    assert code(service.discover, owner, made["id"]) == "busy"
    release.set()
    worker.join(10)
    assert service.freshness(owner, made["id"])["state"] == "current"


# ---------- the rows M4.1b made become snapshot 1 ----------


@pytest.mark.skipif(
    os.environ.get("RAG_TEST_HARDENED") == "1",
    reason="builds a schema of its own and runs the old migrations by hand, as the other migration tests do",
)
def test_discovered_rows_of_the_previous_version_become_snapshot_1_and_the_next_discovery_adopts_them(
    keys, events, driver, owner
):
    schema = f"legacy_{uuid.uuid4().hex[:8]}"
    migrations = sorted(db.MIGRATIONS.glob("*.sql"))
    with admin() as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"SET search_path = {schema}")
        for path in migrations:
            if path.stem >= "0007":
                break
            conn.execute(path.read_text(encoding="utf-8"))
        local = conn.execute("SELECT id FROM principals WHERE kind = 'local-owner'").fetchone()["id"]
        connection_id = conn.execute(
            "INSERT INTO connections (owner_principal_id, name, provider, host, port, database_name, username, "
            "credential_ciphertext, credential_nonce, credential_key_id, ssl_mode, schema_discovered_at) VALUES "
            "(%s, 'old', 'postgresql', 'db.example.org', 5432, 'sales', 'u', '\\x00', '\\x000000000000000000000000', "
            "'k1', "
            "'verify-full', now() - interval '2 days') RETURNING id",
            (local,),
        ).fetchone()["id"]
        for s, name, kind in SAMPLE["objects"]:
            oid = conn.execute(
                "INSERT INTO connection_objects (connection_id, schema_name, object_name, object_type) VALUES (%s, "
                "%s, %s, %s) RETURNING id",
                (connection_id, s, name, kind),
            ).fetchone()["id"]
            for c in SAMPLE["columns"]:
                if (c[0], c[1]) == (s, name):
                    conn.execute(
                        "INSERT INTO connection_columns (object_id, ordinal, column_name, data_type, is_nullable) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (oid, c[2], c[3], c[4], c[5]),
                    )
            for c in SAMPLE["constraints"]:
                if (c[0], c[1]) == (s, name):
                    conn.execute(
                        "INSERT INTO connection_constraints (object_id, kind, constraint_name, columns, ref_schema, "
                        "ref_object, ref_columns) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (oid, c[2], c[3], c[4], c[5], c[6], c[7]),
                    )
            for i in SAMPLE["indexes"]:
                if (i[0], i[1]) == (s, name):
                    conn.execute(
                        "INSERT INTO connection_indexes (object_id, index_name, columns, is_unique, is_primary) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (oid, i[2], i[3], i[4], i[5]),
                    )
        conn.execute(
            "INSERT INTO connection_descriptions (connection_id, target_kind, description) VALUES (%s, 'database', "
            "'kept')",
            (connection_id,),
        )
        for path in migrations:
            if path.stem >= "0007":
                conn.execute(path.read_text(encoding="utf-8"))
        old = conn.execute(
            "SELECT s.version, s.fingerprint, s.fingerprint_version, s.objects_count, s.columns_count, "
            "c.current_snapshot_id = s.id AS current, c.context_confirmed_version FROM schema_snapshots s "
            "JOIN connections c ON c.id = s.connection_id WHERE c.id = %s",
            (connection_id,),
        ).fetchone()
    try:
        assert dict(old) == {
            "version": 1,
            "fingerprint": "",
            "fingerprint_version": 0,
            "objects_count": 6,
            "columns_count": 11,
            "current": True,
            "context_confirmed_version": 1,
        }
        original = (config.DB_SCHEMA, config.DB_HARDENED)
        config.DB_SCHEMA = schema
        db.close()
        try:
            adopted = Service(keys.vault, events, resolver=resolver(), internal=internal, driver_lookup=driver)
            assert adopted.objects(str(local), str(connection_id))[1] == 6  # still readable through the new queries
            assert [d["description"] for d in adopted.descriptions(str(local), str(connection_id))] == ["kept"]
            sealed = keys.vault.seal("a password", str(connection_id))
            with admin() as conn:
                conn.execute(f"SET search_path = {schema}")
                conn.execute(
                    "UPDATE connections SET credential_ciphertext = %s, credential_nonce = %s, credential_key_id = "
                    "%s WHERE id = %s",
                    (sealed.ciphertext, sealed.nonce, sealed.key_id, connection_id),
                )
            again = adopted.discover(str(local), str(connection_id))["snapshot"]
            assert (
                again["changed"] is False and again["version"] == 1
            )  # the same structure: its fingerprint is computed now
            shown = adopted.snapshots(str(local), str(connection_id))[0]
            assert (
                shown["fingerprint"] == again["fingerprint"] != ""
                and shown["fingerprint_version"] == schema_model.FINGERPRINT_VERSION
            )
        finally:
            config.DB_SCHEMA = original[0]
            db.close()
    finally:
        with admin() as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


def test_the_driver_error_type_is_not_needed_to_describe_a_failure(service, owner, driver):
    made = service.create(owner, PAYLOAD)
    driver.sample = sample()
    driver.discover_error = "timeout"
    with pytest.raises(ConnectorError) as failed:
        service.discover(owner, made["id"])
    assert failed.value.code == "discovery_failed" and isinstance(DriverError("timeout"), Exception)
    assert isinstance(psycopg.errors.UniqueViolation, type)


# ---------- changes made while a discovery runs: refused at once, nothing waits for its lock ----------


def test_while_a_discovery_runs_short_changes_are_busy_at_once_and_reads_are_not_blocked(service, owner, driver):
    import time

    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    inside, release = threading.Event(), threading.Event()
    original = driver.introspect

    def slow(target, password, sink, timeout):
        inside.set()
        release.wait(10)
        return original(target, password, sink, timeout)

    driver.introspect = slow
    worker = threading.Thread(target=lambda: service.discover(owner, made["id"]))
    worker.start()
    try:
        assert inside.wait(5)
        started = time.monotonic()
        refused = {
            "describe": lambda: service.describe(owner, made["id"], "database", "", "", "", "hello"),
            "selection": lambda: service.put_selections(owner, made["id"], []),
            "acknowledge": lambda: service.acknowledge(owner, made["id"], 1),
            "update": lambda: service.update(owner, made["id"], {"description": "x"}),
            "disable": lambda: service.set_enabled(owner, made["id"], False),
            "delete": lambda: service.delete(owner, made["id"]),
        }
        for name, call in refused.items():
            assert code(call) == "busy", name
        for read in (service.get, service.freshness, service.selections, service.snapshots, service.descriptions):
            read(owner, made["id"])
        assert time.monotonic() - started < 3  # nothing waited for the discovery's lock
    finally:
        release.set()
        worker.join(15)
    assert service.get(owner, made["id"])["is_enabled"] is True  # none of the refused changes was made


def test_a_lock_held_by_another_process_is_busy_after_a_short_wait_not_a_hang(service, owner, monkeypatch):
    """Another process (a second API worker) is discovering: this one has no in-memory sign of it, only the row lock."""
    import time

    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    monkeypatch.setattr(snap, "LOCK_WAIT", "300ms")
    with admin() as other:
        other.autocommit = False
        other.execute("SELECT id FROM connections WHERE id = %s FOR UPDATE", (made["id"],))
        try:
            for name, call in {
                "describe": lambda: service.describe(owner, made["id"], "database", "", "", "", "hello"),
                "selection": lambda: service.put_selections(owner, made["id"], []),
                "acknowledge": lambda: service.acknowledge(owner, made["id"], 1),
            }.items():
                started = time.monotonic()
                assert code(call) == "busy", name
                assert time.monotonic() - started < 3, name
        finally:
            other.rollback()
    service.describe(owner, made["id"], "database", "", "", "", "hello")  # the lock is gone: it works
