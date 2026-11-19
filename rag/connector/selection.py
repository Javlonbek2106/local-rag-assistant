"""Which objects of an external database may appear in AI context (roadmap M4.2). Pure: no database.

The owner's selection is a list of items. Every item is one of:

    {"kind": "schema", "schema": S}                               include everything in schema S, now AND later
    {"kind": "table", "schema": S, "object": T, "mode": "include"|"exclude"}      one table or view
    {"kind": "column", "schema": S, "object": T, "column": C, "mode": "include"|"exclude"}

Inheritance (the whole of it):
1. A table is INCLUDED AS A WHOLE when it has an include item, or when its schema has an item and the table has no
   exclude item. Its columns are all of its columns except those with a column exclude item.
2. A table with no include (and whose schema has no item) but with column include items is PARTIAL: exactly those
   columns (the context adds its key columns, see context.py).
3. Everything else is not selected. A table EXCLUDE item only means something under a schema item.
4. A schema item is the only way something that does not exist yet becomes selected: objects that appear later are NOT
   selected unless their schema has an item. Selecting a table does not select its schema, and the reverse is not
   automatic either.

Some combinations would mean two things at once, so they are refused instead of being given a meaning:
- the same item twice;                                           (duplicate_item)
- a table exclude whose schema has no item;                      (exclude_without_scope)
- a column exclude on a table that is not included as a whole;   (exclude_without_table)
- a column include on a table that is included as a whole;       (column_include_in_included_table)
- a column include on an excluded table.                         (column_include_in_excluded_table)

What happens to items whose target is gone is decided elsewhere: they are kept and shown as unavailable.
"""

from rag import config
from rag.connector.errors import ConnectorError

KINDS = ("schema", "table", "column")
MODES = ("include", "exclude")
MAX_NAME = 300


def _name(value):
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_NAME or "\x00" in value:
        raise ConnectorError("invalid_selection", 400, reason="bad_name")
    return value


def normalise(raw_items):
    """The items as one canonical list (sorted, complete fields), or ConnectorError("invalid_selection", reason=...)."""
    if not isinstance(raw_items, list) or len(raw_items) > config.CONNECTOR_MAX_SELECTION_ITEMS:
        raise ConnectorError("invalid_selection", 400, reason="too_many_items")
    items = []
    for raw in raw_items:
        if not isinstance(raw, dict) or set(raw) - {"kind", "schema", "object", "column", "mode"}:
            raise ConnectorError("invalid_selection", 400, reason="bad_item")
        kind = raw.get("kind")
        if kind not in KINDS:
            raise ConnectorError("invalid_selection", 400, reason="bad_kind")
        mode = raw.get("mode", "include")
        if mode not in MODES or (kind == "schema" and mode != "include"):
            raise ConnectorError("invalid_selection", 400, reason="bad_mode")
        schema = _name(raw.get("schema"))
        obj = _name(raw.get("object")) if kind != "schema" else ""
        column = _name(raw.get("column")) if kind == "column" else ""
        if kind == "schema" and (raw.get("object") or raw.get("column")):
            raise ConnectorError("invalid_selection", 400, reason="bad_item")
        if kind == "table" and raw.get("column"):
            raise ConnectorError("invalid_selection", 400, reason="bad_item")
        items.append({"kind": kind, "schema": schema, "object": obj, "column": column, "mode": mode})
    keys = [(i["kind"], i["schema"], i["object"], i["column"]) for i in items]
    if len(set(keys)) != len(keys):
        raise ConnectorError("invalid_selection", 400, reason="duplicate_item")
    items.sort(key=lambda i: (i["schema"].encode(), i["object"].encode(), i["column"].encode(), i["kind"]))
    Selection(items).check()
    return items


class Selection:
    """The items, ready to be asked about one table or column at a time."""

    def __init__(self, items):
        self.items = list(items)
        self.schemas = {i["schema"] for i in self.items if i["kind"] == "schema"}
        self._tables = {(i["schema"], i["object"]): i["mode"] for i in self.items if i["kind"] == "table"}
        self._columns = {}
        for i in self.items:
            if i["kind"] == "column":
                self._columns.setdefault((i["schema"], i["object"]), {})[i["column"]] = i["mode"]

    def check(self):
        """Refuse the combinations that would mean two things (see the module text)."""
        for (schema, _obj), mode in self._tables.items():
            if mode == "exclude" and schema not in self.schemas:
                raise ConnectorError("invalid_selection", 400, reason="exclude_without_scope")
        for (schema, obj), columns in self._columns.items():
            whole = self.whole(schema, obj)
            excluded = self._tables.get((schema, obj)) == "exclude"
            for mode in columns.values():
                if mode == "exclude" and not whole:
                    raise ConnectorError("invalid_selection", 400, reason="exclude_without_table")
                if mode == "include" and whole:
                    raise ConnectorError("invalid_selection", 400, reason="column_include_in_included_table")
                if mode == "include" and excluded:
                    raise ConnectorError("invalid_selection", 400, reason="column_include_in_excluded_table")

    def whole(self, schema, obj):
        """The table is included as a whole (rule 1)."""
        mode = self._tables.get((schema, obj))
        return mode == "include" or (mode is None and schema in self.schemas)

    def state(self, schema, obj):
        """("all", excluded column names) | ("columns", included column names) | None (not selected)."""
        columns = self._columns.get((schema, obj), {})
        if self.whole(schema, obj):
            return ("all", frozenset(c for c, m in columns.items() if m == "exclude"))
        included = frozenset(c for c, m in columns.items() if m == "include")
        if included and self._tables.get((schema, obj)) != "exclude":
            return ("columns", included)
        return None

    def selected(self, schema, obj):
        return self.state(schema, obj) is not None

    def column_selected(self, schema, obj, column, key_columns=frozenset()):
        """The column is part of the selection. `key_columns` are the table's key columns, which a partially selected
        table gets in addition (context.py)."""
        state = self.state(schema, obj)
        if state is None:
            return False
        kind, names = state
        return column not in names if kind == "all" else (column in names or column in key_columns)

    def available(self, objects, columns):
        """The items whose target exists, given the set of (schema, object) and of (schema, object, column) that exist.
        Returns (item, bool) for every item, in the order of `items`."""
        schemas = {s for s, _ in objects}
        out = []
        for i in self.items:
            if i["kind"] == "schema":
                ok = i["schema"] in schemas
            elif i["kind"] == "table":
                ok = (i["schema"], i["object"]) in objects
            else:
                ok = (i["schema"], i["object"], i["column"]) in columns
            out.append((i, ok))
        return out


def affects(selection, change, key_columns=frozenset()):
    """Does one recorded change touch what the selection includes? `change`: object_type, schema_name, object_name,
    sub_name. A change of the schema itself touches a selected schema; of a table or view, that table; of a column, that
    column (or, for a table included as a whole, the table: a new column is new context); of a key, index or
    relationship, the table (these are part of what is described)."""
    kind, schema, obj, sub = change["object_type"], change["schema_name"], change["object_name"], change["sub_name"]
    if kind == "schema":
        return schema in selection.schemas or any(i["schema"] == schema for i in selection.items)
    if not selection.selected(schema, obj):
        return False
    if kind == "column":
        return selection.column_selected(schema, obj, sub, key_columns)
    return True
