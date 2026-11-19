"""M4.2: which objects may appear in AI context: the inheritance rules, the refused combinations, availability."""

import pytest

from rag import config
from rag.connector import selection as sel
from rag.connector.errors import ConnectorError


def item(kind, schema, obj="", column="", mode="include"):
    return {"kind": kind, "schema": schema, "object": obj, "column": column, "mode": mode}


def schema(name):
    return item("schema", name)


def table(s, o, mode="include"):
    return item("table", s, o, mode=mode)


def column(s, o, c, mode="include"):
    return item("column", s, o, c, mode)


def refused(items):
    with pytest.raises(ConnectorError) as error:
        sel.normalise(items)
    assert error.value.code == "invalid_selection" and error.value.status == 400
    return error.value.reason


def chosen(*items):
    return sel.Selection(sel.normalise(list(items)))


# ---------- inheritance ----------


def test_nothing_selected_is_the_default_and_selects_nothing():
    s = chosen()
    assert not s.selected("a", "t") and s.state("a", "t") is None and s.items == []


def test_a_table_include_selects_it_with_all_its_columns():
    s = chosen(table("a", "t"))
    assert s.state("a", "t") == ("all", frozenset()) and s.selected("a", "t")
    assert not s.selected("a", "other") and not s.selected("b", "t")  # no other table, no same name in another schema
    assert s.column_selected("a", "t", "anything")


def test_a_schema_item_selects_every_table_in_it_now_and_later_but_no_other_schema():
    s = chosen(schema("sales"))
    assert s.selected("sales", "existing") and s.selected("sales", "added_next_year") and not s.selected("hr", "t")


def test_a_table_exclude_removes_one_table_from_a_selected_schema():
    s = chosen(schema("sales"), table("sales", "audit", "exclude"))
    assert s.selected("sales", "customer") and not s.selected("sales", "audit") and s.state("sales", "audit") is None


def test_a_table_include_inside_a_selected_schema_is_allowed_and_changes_nothing():
    assert chosen(schema("sales"), table("sales", "t")).state("sales", "t") == ("all", frozenset())


def test_a_column_exclude_removes_a_column_of_an_included_table():
    s = chosen(table("a", "t"), column("a", "t", "secret", "exclude"))
    assert s.state("a", "t") == ("all", frozenset({"secret"}))
    assert s.column_selected("a", "t", "id") and not s.column_selected("a", "t", "secret")
    via_schema = chosen(schema("a"), column("a", "t", "secret", "exclude"))
    assert not via_schema.column_selected("a", "t", "secret") and via_schema.column_selected("a", "u", "secret")


def test_column_includes_alone_select_a_table_partially_and_only_those_columns():
    s = chosen(column("a", "t", "id"), column("a", "t", "name"))
    assert s.state("a", "t") == ("columns", frozenset({"id", "name"})) and s.selected("a", "t")
    assert s.column_selected("a", "t", "id") and not s.column_selected("a", "t", "other")


def test_a_partial_table_also_gets_its_key_columns_when_the_caller_says_which_they_are():
    s = chosen(column("a", "t", "name"))
    assert s.column_selected("a", "t", "pk_id", key_columns=frozenset({"pk_id"})) and not s.column_selected(
        "a", "t", "pk_id"
    )


def test_selecting_a_table_does_not_select_its_schema_and_the_reverse_is_not_automatic_for_columns():
    s = chosen(table("a", "t"))
    assert not s.selected("a", "u") and "a" not in s.schemas


# ---------- refused combinations ----------


def test_the_same_item_twice_is_refused():
    assert refused([table("a", "t"), table("a", "t")]) == "duplicate_item"
    assert refused([table("a", "t"), table("a", "t", "exclude")]) == "duplicate_item"


def test_a_table_exclude_without_its_schema_selected_is_refused():
    assert refused([table("a", "t", "exclude")]) == "exclude_without_scope"


def test_a_column_exclude_on_a_table_that_is_not_included_as_a_whole_is_refused():
    assert refused([column("a", "t", "c", "exclude")]) == "exclude_without_table"
    assert (
        refused([schema("a"), table("a", "t", "exclude"), column("a", "t", "c", "exclude")]) == "exclude_without_table"
    )
    assert refused([column("a", "t", "c", "exclude"), column("a", "t", "d")]) == "exclude_without_table"


def test_a_column_include_on_a_table_included_as_a_whole_is_refused():
    assert refused([table("a", "t"), column("a", "t", "c")]) == "column_include_in_included_table"
    assert refused([schema("a"), column("a", "t", "c")]) == "column_include_in_included_table"


def test_a_column_include_on_an_excluded_table_is_refused():
    assert (
        refused([schema("a"), table("a", "t", "exclude"), column("a", "t", "c")]) == "column_include_in_excluded_table"
    )


@pytest.mark.parametrize(
    "bad,reason",
    [
        ("nope", "too_many_items"),
        ([5], "bad_item"),
        ([{"kind": "planet", "schema": "a"}], "bad_kind"),
        ([{"kind": "schema", "schema": "a", "mode": "exclude"}], "bad_mode"),
        ([{"kind": "table", "schema": "a", "object": "t", "mode": "maybe"}], "bad_mode"),
        ([{"kind": "schema", "schema": ""}], "bad_name"),
        ([{"kind": "table", "schema": "a"}], "bad_name"),
        ([{"kind": "column", "schema": "a", "object": "t"}], "bad_name"),
        ([{"kind": "schema", "schema": "a" * 301}], "bad_name"),
        ([{"kind": "schema", "schema": "a\x00"}], "bad_name"),
        ([{"kind": "schema", "schema": "a", "object": "t"}], "bad_item"),
        ([{"kind": "table", "schema": "a", "object": "t", "column": "c"}], "bad_item"),
        ([{"kind": "schema", "schema": "a", "owner": "x"}], "bad_item"),
    ],
)
def test_a_malformed_item_is_refused_with_its_reason(bad, reason):
    assert refused(bad) == reason


def test_the_number_of_items_is_bounded(monkeypatch):
    monkeypatch.setattr(config, "CONNECTOR_MAX_SELECTION_ITEMS", 3)
    assert refused([table("a", str(i)) for i in range(4)]) == "too_many_items"
    assert len(sel.normalise([table("a", str(i)) for i in range(3)])) == 3


def test_the_default_mode_is_include_and_the_result_is_sorted_and_complete():
    items = sel.normalise([{"kind": "table", "schema": "b", "object": "z"}, {"kind": "schema", "schema": "a"}])
    assert items == [schema("a"), table("b", "z")] and all(
        set(i) == {"kind", "schema", "object", "column", "mode"} for i in items
    )


def test_the_order_of_the_items_given_does_not_matter():
    a = [schema("s"), table("s", "x", "exclude"), table("t", "y"), column("u", "z", "c")]
    assert sel.normalise(a) == sel.normalise(list(reversed(a)))


# ---------- availability ----------


def test_items_whose_target_is_gone_are_reported_unavailable_and_not_removed():
    s = chosen(schema("sales"), table("hr", "gone"), column("hr", "emp", "email"), column("hr", "emp", "old"))
    objects = {("sales", "t"), ("hr", "emp")}
    columns = {("hr", "emp", "email")}
    state = {(i["kind"], i["schema"], i["object"], i["column"]): ok for i, ok in s.available(objects, columns)}
    assert state[("schema", "sales", "", "")] is True
    assert state[("table", "hr", "gone", "")] is False
    assert state[("column", "hr", "emp", "email")] is True and state[("column", "hr", "emp", "old")] is False
    assert len(s.items) == 4  # nothing was dropped


# ---------- which recorded changes touch the selection ----------


def change(kind, schema_name, obj="", sub="", how="modified"):
    return {"object_type": kind, "schema_name": schema_name, "object_name": obj, "sub_name": sub, "change_type": how}


def test_a_change_touches_the_selection_only_where_the_selection_is():
    s = chosen(table("sales", "customer"), column("hr", "emp", "email"))
    assert sel.affects(s, change("table", "sales", "customer", how="removed"))
    assert not sel.affects(s, change("table", "sales", "other", how="added"))  # a new table nobody selected
    assert sel.affects(
        s, change("column", "sales", "customer", "age", "added")
    )  # a new column of a whole table is new context
    assert sel.affects(s, change("relationship", "sales", "customer", "fk"))
    assert sel.affects(s, change("index", "sales", "customer", "ix"))
    assert sel.affects(s, change("column", "hr", "emp", "email", "modified"))
    assert not sel.affects(
        s, change("column", "hr", "emp", "phone", "added")
    )  # a column of a partial table that was not chosen
    assert sel.affects(
        s, change("column", "hr", "emp", "id", "modified"), key_columns=frozenset({"id"})
    )  # but its key is part of it
    assert not sel.affects(s, change("table", "audit", "log"))


def test_a_changed_column_that_was_excluded_does_not_touch_the_selection():
    s = chosen(table("a", "t"), column("a", "t", "secret", "exclude"))
    assert not sel.affects(s, change("column", "a", "t", "secret", "modified"))
    assert sel.affects(s, change("column", "a", "t", "other", "modified"))


def test_a_schema_change_touches_a_selection_that_has_anything_in_the_schema():
    assert sel.affects(chosen(schema("sales")), change("schema", "sales", how="removed"))
    assert sel.affects(chosen(table("sales", "t")), change("schema", "sales", how="removed"))
    assert not sel.affects(chosen(table("hr", "t")), change("schema", "sales", how="added"))
