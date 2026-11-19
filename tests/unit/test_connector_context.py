"""M4.2: the schema context builder: what is in it, what never is, determinism, and the size budget."""

import json
import random

import pytest

from rag import config
from rag.connector import context as ctx
from rag.connector import selection as sel

CURRENT = {"state": "current", "reasons": [], "has_snapshot": True}
SNAPSHOT = {"version": 3, "fingerprint": "f" * 64, "verified_at": "2026-11-20T10:00:00Z"}


def item(kind, schema, obj="", column="", mode="include"):
    return {"kind": kind, "schema": schema, "object": obj, "column": column, "mode": mode}


def selected(*items):
    return sel.Selection(sel.normalise(list(items)))


def pk(columns, name="pk"):
    return {
        "kind": "primary_key",
        "name": name,
        "columns": columns,
        "ref_schema": None,
        "ref_object": None,
        "ref_columns": [],
    }


def fk(name, columns, ref_schema, ref_object, ref_columns):
    return {
        "kind": "foreign_key",
        "name": name,
        "columns": columns,
        "ref_schema": ref_schema,
        "ref_object": ref_object,
        "ref_columns": ref_columns,
    }


def unique(name, columns):
    return {
        "kind": "unique",
        "name": name,
        "columns": columns,
        "ref_schema": None,
        "ref_object": None,
        "ref_columns": [],
    }


class Source:
    """A snapshot in memory. `shuffle` lists things in another order each time to prove the builder does not care."""

    provider, database_name, snapshot = "postgresql", "salesdb", SNAPSHOT

    def __init__(self, shuffle=None):
        self.descriptions = {}
        self._objects = [
            ("sales", "customer", "table"),
            ("sales", "order_line", "table"),
            ("sales", "order_note", "table"),
            ("sales", "big_orders", "view"),
            ("hr", "employee", "table"),
            ("audit", "log", "table"),
        ]
        self._columns = {
            ("sales", "customer"): [("id", "integer", False), ("name", "text", False), ("secret_note", "text", True)],
            ("sales", "order_line"): [
                ("order_id", "integer", False),
                ("line_no", "integer", False),
                ("customer_id", "integer", False),
                ("qty", "integer", False),
            ],
            ("sales", "order_note"): [
                ("order_id", "integer", False),
                ("line_no", "integer", False),
                ("text", "text", True),
            ],
            ("sales", "big_orders"): [("order_id", "integer", False), ("qty", "integer", False)],
            ("hr", "employee"): [("id", "integer", False), ("email", "text", False), ("manager_id", "integer", True)],
            ("audit", "log"): [("id", "integer", False), ("who", "text", True)],
        }
        self._constraints = {
            ("sales", "customer"): [pk(["id"], "pk_customer")],
            ("sales", "order_line"): [
                pk(["order_id", "line_no"], "pk_ol"),
                fk("fk_customer", ["customer_id"], "sales", "customer", ["id"]),
            ],
            ("sales", "order_note"): [
                fk("fk_line", ["order_id", "line_no"], "sales", "order_line", ["order_id", "line_no"])
            ],
            ("hr", "employee"): [
                pk(["id"], "pk_emp"),
                unique("uq_email", ["email"]),
                fk("fk_mgr", ["manager_id"], "hr", "employee", ["id"]),
            ],
            ("audit", "log"): [pk(["id"], "pk_log"), fk("fk_who", ["who"], "hr", "employee", ["email"])],
        }
        if shuffle is not None:
            shuffle.shuffle(self._objects)
            for lst in self._constraints.values():
                shuffle.shuffle(lst)

    def objects(self):
        return list(self._objects)

    def constraints(self, wanted):
        return {k: [dict(c) for c in v] for k, v in self._constraints.items() if k in wanted}

    def columns(self, batch):
        return {k: list(self._columns[k]) for k in batch}


def build(source, chosen, fresh=CURRENT, **kwargs):
    return ctx.build(source, chosen, fresh, **kwargs)


def schemas_of(result):
    return {s["name"]: {o["name"]: o for o in s["objects"]} for s in result["context"]["schemas"]}


def names(result):
    return {(s, o) for s, objs in schemas_of(result).items() for o in objs}


# ---------- what is in it ----------


def test_only_the_selected_objects_and_columns_are_in_it():
    chosen = selected(
        item("schema", "sales"),
        item("table", "sales", "order_note", mode="exclude"),
        item("column", "sales", "customer", "secret_note", "exclude"),
    )
    result = build(Source(), chosen)
    shown = schemas_of(result)
    assert set(shown) == {"sales"} and set(shown["sales"]) == {"customer", "order_line", "big_orders"}
    assert [c["name"] for c in shown["sales"]["customer"]["columns"]] == ["id", "name"]  # secret_note was excluded
    assert shown["sales"]["big_orders"]["type"] == "view"
    assert result["status"] == "ok" and result["usable"] is True and result["complete"] is True


def test_a_primary_key_unique_keys_and_foreign_keys_are_described():
    result = build(Source(), selected(item("schema", "sales"), item("schema", "hr")))
    line = schemas_of(result)["sales"]["order_line"]
    assert line["primary_key"] == ["order_id", "line_no"]
    assert line["foreign_keys"] == [
        {
            "name": "fk_customer",
            "columns": ["customer_id"],
            "references": {"schema": "sales", "object": "customer", "columns": ["id"]},
        }
    ]
    employee = schemas_of(result)["hr"]["employee"]
    assert (
        employee["unique"] == [["email"]] and employee["foreign_keys"][0]["references"]["object"] == "employee"
    )  # a self reference
    flags = {c["name"]: c for c in line["columns"]}
    assert flags["order_id"]["pk"] is True and flags["customer_id"]["fk"] is True and "pk" not in flags["qty"]


def test_a_foreign_key_to_something_not_selected_is_left_out_and_only_counted():
    result = build(Source(), selected(item("table", "sales", "order_note"), item("table", "audit", "log")))
    assert "foreign_keys" not in schemas_of(result)["sales"]["order_note"]  # its target order_line is not selected
    assert result["context"]["omitted"]["relationships_hidden"] == 2
    text = json.dumps(result)
    assert "order_line" not in text and "employee" not in text  # the name of an unselected object is never revealed


def test_nothing_unselected_appears_anywhere_in_the_output():
    result = build(Source(), selected(item("table", "hr", "employee")))
    text = json.dumps(result)
    for hidden in ("customer", "order_line", "secret_note", "audit", "salesdb_secret"):
        assert hidden not in text
    assert result["context"]["database"] == {"name": "salesdb"}


def test_a_partially_selected_table_has_only_the_chosen_columns_plus_its_keys_flagged_implicit():
    result = build(Source(), selected(item("column", "sales", "order_line", "qty"), item("table", "sales", "customer")))
    line = schemas_of(result)["sales"]["order_line"]
    by_name = {c["name"]: c for c in line["columns"]}
    assert set(by_name) == {
        "order_id",
        "line_no",
        "customer_id",
        "qty",
    }  # qty chosen; the primary key and the foreign key come with it
    assert (
        "implicit" not in by_name["qty"]
        and by_name["order_id"]["implicit"] is True
        and by_name["customer_id"]["implicit"] is True
    )
    assert line["primary_key"] == ["order_id", "line_no"] and line["foreign_keys"][0]["name"] == "fk_customer"


def test_an_explicitly_excluded_column_wins_and_a_key_that_needs_it_is_not_described():
    result = build(
        Source(),
        selected(item("table", "sales", "order_line"), item("column", "sales", "order_line", "line_no", "exclude")),
    )
    line = schemas_of(result)["sales"]["order_line"]
    assert "line_no" not in [c["name"] for c in line["columns"]] and "primary_key" not in line
    assert result["context"]["omitted"]["relationships_hidden"] == 1  # the foreign key to a table not selected


def test_a_foreign_key_whose_target_column_is_not_shown_is_not_described():
    chosen = selected(
        item("table", "sales", "order_line"),
        item("table", "sales", "customer"),
        item("column", "sales", "customer", "id", "exclude"),
    )
    result = build(Source(), chosen)
    assert "foreign_keys" not in schemas_of(result)["sales"]["order_line"]


def test_descriptions_are_included_for_what_is_shown_and_the_database():
    source = Source()
    source.descriptions = {
        ("database",): "the sales database",
        ("schema", "sales"): "everything about selling",
        ("table", "sales", "customer"): "people who buy",
        ("column", "sales", "customer", "name"): "the legal name",
        ("table", "hr", "employee"): "not selected, so not shown",
    }
    result = build(source, selected(item("schema", "sales")))
    assert result["context"]["database"]["description"] == "the sales database"
    sales = result["context"]["schemas"][0]
    assert sales["description"] == "everything about selling"
    customer = {o["name"]: o for o in sales["objects"]}["customer"]
    assert customer["description"] == "people who buy"
    assert {c["name"]: c.get("description") for c in customer["columns"]}["name"] == "the legal name"
    assert "not selected" not in json.dumps(result)


def test_no_row_credential_or_connection_string_can_be_in_it():
    text = json.dumps(build(Source(), selected(item("schema", "sales"), item("schema", "hr"))))
    for forbidden in ("password", "ciphertext", "postgresql://", "host", "username", "default"):
        assert forbidden not in text


# ---------- determinism ----------


def test_the_same_inputs_give_the_same_bytes():
    chosen = selected(item("schema", "sales"), item("schema", "hr"))
    first, second = build(Source(), chosen), build(Source(), chosen)
    assert ctx.dumps(first) == ctx.dumps(second)
    assert first["budget"]["context_fingerprint"] == second["budget"]["context_fingerprint"]


def test_the_order_the_snapshot_lists_things_in_does_not_change_the_output():
    chosen = selected(item("schema", "sales"), item("schema", "hr"), item("table", "audit", "log"))
    reference = ctx.dumps(build(Source(), chosen))
    for seed in range(15):
        assert ctx.dumps(build(Source(random.Random(seed)), chosen)) == reference


def test_schemas_and_objects_come_out_in_byte_order_and_keys_in_sorted_order():
    result = build(Source(), selected(item("schema", "sales"), item("schema", "hr"), item("schema", "audit")))
    assert [s["name"] for s in result["context"]["schemas"]] == ["audit", "hr", "sales"]
    assert [o["name"] for o in result["context"]["schemas"][2]["objects"]] == [
        "big_orders",
        "customer",
        "order_line",
        "order_note",
    ]
    text = ctx.dumps(result)
    assert " " not in text.replace("sales database", "")  # no insignificant whitespace


def test_a_change_of_the_selection_changes_the_fingerprint_of_the_context():
    a = build(Source(), selected(item("schema", "sales")))
    b = build(Source(), selected(item("schema", "sales"), item("table", "sales", "customer", mode="exclude")))
    assert a["budget"]["context_fingerprint"] != b["budget"]["context_fingerprint"]


# ---------- freshness and emptiness ----------


def test_an_empty_selection_is_an_explicit_empty_context():
    result = build(Source(), selected())
    assert result["status"] == "empty" and result["usable"] is False and result["context"]["schemas"] == []
    assert result["selection"]["selected_objects"] == 0


@pytest.mark.parametrize("state", ["stale", "failed", "in_progress", "never_discovered"])
def test_a_context_that_is_not_current_says_so_and_is_not_usable(state):
    result = build(
        Source(), selected(item("schema", "sales")), {"state": state, "reasons": ["x"], "has_snapshot": True}
    )
    assert result["status"] == state and result["usable"] is False and result["freshness"]["state"] == state
    assert result["context"]["schemas"]  # what the last good snapshot says is still shown, clearly marked


def test_without_a_snapshot_there_is_nothing_to_show():
    class Nothing(Source):
        snapshot = None

    result = build(
        Nothing(),
        selected(item("schema", "sales")),
        {"state": "never_discovered", "reasons": [], "has_snapshot": False},
    )
    assert result["context"]["schemas"] == [] and result["snapshot"] is None and result["usable"] is False


def test_selected_things_that_are_gone_are_listed_and_are_not_in_the_context():
    chosen = selected(item("table", "sales", "customer"), item("table", "sales", "dropped"))
    result = build(Source(), chosen, unavailable=chosen.available({("sales", "customer")}, set()))
    assert names(result) == {("sales", "customer")}
    assert result["selection"]["unavailable_count"] == 1
    assert result["selection"]["unavailable"] == [
        {"kind": "table", "schema": "sales", "object": "dropped", "column": ""}
    ]


# ---------- the size budget ----------


def test_a_small_budget_omits_whole_objects_never_part_of_one_and_says_so():
    chosen = selected(item("schema", "sales"), item("schema", "hr"))
    full = build(Source(), chosen)
    size = full["budget"]["used_chars"]
    small = build(Source(), chosen, max_chars=size // 2)
    assert small["complete"] is False and small["budget"]["used_chars"] <= size // 2
    assert small["context"]["omitted"]["count"] == len(names(full)) - len(names(small)) > 0
    for schema_name, objects in schemas_of(small).items():
        for name, block in objects.items():
            assert (
                block == schemas_of(full)[schema_name][name] or "foreign_keys" in schemas_of(full)[schema_name][name]
            )  # whole, never cut
    assert small["context"]["omitted"]["reason"] == "budget"
    assert all(len(pair) == 2 for pair in small["context"]["omitted"]["objects"])  # [schema, name]


def test_the_budget_is_never_exceeded_whatever_it_is():
    chosen = selected(item("schema", "sales"), item("schema", "hr"), item("schema", "audit"))
    for limit in range(200, 3000, 137):
        result = build(Source(), chosen, max_chars=limit)
        assert len(ctx.dumps(result["context"])) <= limit and result["budget"]["used_chars"] == len(
            ctx.dumps(result["context"])
        )


def test_objects_with_the_most_relationships_are_kept_first():
    chosen = selected(item("schema", "sales"), item("schema", "hr"), item("schema", "audit"))
    full = build(Source(), chosen)
    tight = build(Source(), chosen, max_chars=full["budget"]["used_chars"] // 2)
    kept = names(tight)
    assert {("sales", "order_line"), ("hr", "employee")} <= kept  # the hubs of the join graph
    assert ("sales", "big_orders") not in kept  # a view without relationships goes first


def test_with_room_for_nothing_the_status_says_the_budget_is_too_small():
    result = build(Source(), selected(item("schema", "sales")), max_chars=200)
    assert result["status"] == "budget_too_small" and result["usable"] is False and result["complete"] is False
    assert result["context"]["schemas"] == [] and result["context"]["omitted"]["count"] == 4


def test_a_foreign_key_to_an_object_dropped_for_size_is_dropped_with_it_and_counted():
    chosen = selected(item("schema", "sales"))
    full = build(Source(), chosen)
    for limit in range(300, full["budget"]["used_chars"], 90):
        result = build(Source(), chosen, max_chars=limit)
        kept = names(result)
        for objects in schemas_of(result).values():
            for block in objects.values():
                for foreign in block.get("foreign_keys", []):
                    assert (foreign["references"]["schema"], foreign["references"]["object"]) in kept


def test_a_primary_key_is_never_dropped_from_an_object_that_is_included():
    chosen = selected(item("schema", "sales"), item("schema", "hr"))
    for limit in range(300, 4000, 211):
        for block in (
            b for objs in schemas_of(build(Source(), chosen, max_chars=limit)).values() for b in objs.values()
        ):
            if block["name"] in ("customer", "order_line", "employee"):
                assert "primary_key" in block


def test_the_budget_asked_for_is_limited_to_the_largest_the_server_allows(monkeypatch):
    monkeypatch.setattr(config, "CONNECTOR_CONTEXT_HARD_MAX_CHARS", 500)
    result = build(Source(), selected(item("schema", "sales")), max_chars=10_000_000)
    assert result["budget"]["max_chars"] == 500 and result["budget"]["used_chars"] <= 500


def test_the_default_budget_is_the_configured_one(monkeypatch):
    monkeypatch.setattr(config, "CONNECTOR_CONTEXT_MAX_CHARS", 1234)
    assert build(Source(), selected(item("schema", "sales")))["budget"]["max_chars"] == 1234


def test_a_big_schema_is_read_in_batches_and_stops_when_the_budget_is_used(monkeypatch):
    class Wide(Source):
        calls = 0

        def __init__(self):
            super().__init__()
            self._objects = [("big", f"t{i:04d}", "table") for i in range(1000)]
            self._columns = {("big", f"t{i:04d}"): [("id", "integer", False), ("v", "text", True)] for i in range(1000)}
            self._constraints = {}

        def columns(self, batch):
            Wide.calls += 1
            assert len(batch) <= ctx.BATCH
            return super().columns(batch)

    result = build(Wide(), selected(item("schema", "big")), max_chars=3000)
    assert result["complete"] is False and result["budget"]["used_chars"] <= 3000
    assert Wide.calls <= 3  # it did not ask for the columns of the whole schema
    assert (
        result["context"]["omitted"]["count"] == 1000 - len(names(result))
        and len(result["context"]["omitted"]["objects"]) == ctx.OMITTED_LISTED
    )
