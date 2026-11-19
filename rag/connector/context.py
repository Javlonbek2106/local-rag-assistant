"""The schema context: a compact, deterministic description of the SELECTED part of an external database (roadmap M4.2).

Pure: it is given a `source` (what the current snapshot says), the owner's `selection`, and the freshness, and returns a
dictionary. It calls no model, reads no row of the external database, and holds no credential.

What is in it, and what is not:
- Only selected tables and views, and in each only the selected columns (selection.py). A partially selected table also
  gets its key columns, flagged "implicit": true, so that its keys and relationships still make sense.
- Primary and unique keys, and foreign keys, of the included objects: a key is shown when ALL its columns are shown; a
  foreign key also when its target is included and its target columns are shown. Foreign keys to anything not included
  are left out and only COUNTED ("relationships_hidden"): the name of an object the owner did not select is not
  revealed.
- The descriptions the owner wrote for the database, a schema, a table or a column, only for what is shown. A
  description of something that no longer exists is not in it (it is orphaned until the object returns).
- Types, nullability. Never a row, a default value, a connection string or a credential.

Size. `budget.max_chars` limits the length of `context` serialised as JSON (the envelope around it is a few hundred
characters and is not counted). An object is never cut: it is in whole or it is listed in `omitted`. Objects are tried
in this order (the policy): the more relationships to other selected objects, the earlier (so the join graph stays
together and the hubs come first); equal ones by schema then name, byte order. An object that does not fit is skipped
and the following ones are still tried. If everything chosen plus the list of omitted objects is still too long, the
lowest priority objects are dropped until it fits. `complete` is false whenever anything was omitted.

Order and bytes: every list is in a defined order and the JSON is written with sorted keys and fixed separators, so the
same snapshot, selection and budget give the same bytes; `budget.context_fingerprint` is the SHA-256 of them.
"""

import hashlib
import json

from rag import config
from rag.connector import freshness as fresh_states

FORMAT = 1
OMITTED_LISTED = 50
UNAVAILABLE_LISTED = 20
MIN_BLOCK = 60  # no object is smaller than this: with less room left, nothing more is tried
BATCH = 100


def dumps(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _order(name):
    return name.encode("utf-8")


def _status(fresh, selected_count):
    state = fresh["state"]
    if state != fresh_states.CURRENT:
        return state
    return "empty" if selected_count == 0 else "ok"


def build(source, selection, fresh, *, max_chars=None, revision=0, unavailable=()):
    """The context envelope. `source` must offer: provider, database_name, snapshot (dict or None), descriptions (dict
    keyed ("database",), ("schema", s), ("table", s, o), ("column", s, o, c)), objects() -> [(schema, name, type)],
    constraints(set of (schema, name)) -> {(schema, name): [constraint dicts]} and columns([(schema, name)]) ->
    {(schema, name): [(column, data_type, nullable), ...]} in ordinal order."""
    max_chars = min(max_chars or config.CONNECTOR_CONTEXT_MAX_CHARS, config.CONNECTOR_CONTEXT_HARD_MAX_CHARS)
    types = {(s, n): t for s, n, t in source.objects()} if source.snapshot else {}
    selected = {key for key in types if selection.selected(*key)}
    constraints = source.constraints(selected) if selected else {}

    key_columns, degree = {}, {}
    for obj, items in constraints.items():
        for c in items:
            if c["kind"] == "primary_key":
                key_columns.setdefault(obj, set()).update(c["columns"])
            elif c["kind"] == "foreign_key" and (c["ref_schema"], c["ref_object"]) in selected:
                key_columns.setdefault(obj, set()).update(c["columns"])
                target = (c["ref_schema"], c["ref_object"])
                degree[obj] = degree.get(obj, 0) + 1
                degree[target] = degree.get(target, 0) + 1

    def visible(obj, column):
        return selection.column_selected(obj[0], obj[1], column, frozenset(key_columns.get(obj, ())))

    descriptions = source.descriptions

    def render(obj, columns, included):
        schema, name = obj
        shown, hidden_columns = [], set()
        for column, data_type, nullable in columns:
            if not visible(obj, column):
                hidden_columns.add(column)
                continue
            entry = {"name": column, "type": data_type, "nullable": bool(nullable)}
            state = selection.state(schema, name)
            if state and state[0] == "columns" and column not in state[1]:
                entry["implicit"] = True
            text = descriptions.get(("column", schema, name, column))
            if text:
                entry["description"] = text
            shown.append(entry)
        names = {c["name"] for c in shown}
        block = {"name": name, "type": types[obj], "columns": shown}
        text = descriptions.get(("table", schema, name))
        if text:
            block["description"] = text
        unique, foreign, hidden = [], [], 0
        for c in sorted(constraints.get(obj, ()), key=lambda c: (c["kind"], _order(c["name"]))):
            cols = list(c["columns"])
            shown_here = all(column in names for column in cols)
            if c["kind"] == "primary_key" and shown_here:
                block["primary_key"] = cols
            elif c["kind"] == "unique" and shown_here:
                unique.append(cols)
            elif c["kind"] == "foreign_key":
                target = (c["ref_schema"], c["ref_object"])
                ok = shown_here and target in included and all(visible(target, rc) for rc in c["ref_columns"])
                if ok:
                    foreign.append(
                        {
                            "name": c["name"],
                            "columns": cols,
                            "references": {
                                "schema": c["ref_schema"],
                                "object": c["ref_object"],
                                "columns": list(c["ref_columns"]),
                            },
                        }
                    )
                else:
                    hidden += 1
        if unique:
            block["unique"] = sorted(unique)
        if foreign:
            block["foreign_keys"] = foreign
        for column in names:
            for fk in foreign:
                if column in fk["columns"]:
                    next(e for e in shown if e["name"] == column)["fk"] = True
            if column in set(block.get("primary_key", ())):
                next(e for e in shown if e["name"] == column)["pk"] = True
        return block, hidden

    def payload_of(chosen_blocks, omitted_names, omitted_count, hidden_relationships, listed=OMITTED_LISTED):
        by_schema = {}
        for (schema, _name), block in chosen_blocks:
            by_schema.setdefault(schema, []).append(block)
        schemas = []
        for schema in sorted(by_schema, key=_order):
            entry = {"name": schema, "objects": sorted(by_schema[schema], key=lambda b: _order(b["name"]))}
            text = descriptions.get(("schema", schema))
            if text:
                entry["description"] = text
            schemas.append(entry)
        database = {"name": source.database_name}
        if descriptions.get(("database",)):
            database["description"] = descriptions[("database",)]
        omitted = {
            "count": omitted_count,
            "objects": omitted_names[:listed],
            "reason": "budget",
            "relationships_hidden": hidden_relationships,
        }
        return {"database": database, "schemas": schemas, "omitted": omitted}

    priority = sorted(selected, key=lambda k: (-degree.get(k, 0), _order(k[0]), _order(k[1])))
    chosen, omitted = [], []  # chosen: [(key, columns)] in priority order
    base = len(dumps(payload_of([], [], 0, 0)))
    used, index, smallest = base, 0, MIN_BLOCK
    while index < len(priority):
        if max_chars - used < smallest:  # not even the smallest object tried so far would fit
            break
        batch = priority[index : index + BATCH]
        index += len(batch)
        columns_of = source.columns(batch)
        for key in batch:
            block, _hidden = render(key, columns_of.get(key, []), selected)
            size = len(dumps(block)) + 1
            smallest = size if smallest == MIN_BLOCK else min(smallest, size)
            if used + size <= max_chars:
                chosen.append((key, columns_of.get(key, [])))
                used += size
            else:
                omitted.append(key)
    omitted += priority[index:]

    def assemble(listed=OMITTED_LISTED):
        blocks, hidden_total = [], 0
        included = {key for key, _ in chosen}
        for key, columns in chosen:
            block, hidden = render(key, columns, included)
            blocks.append((key, block))
            hidden_total += hidden
        names = [[s, n] for s, n in sorted(omitted, key=_order_key)]
        payload = payload_of(blocks, names, len(omitted), hidden_total, listed)
        return payload, hidden_total

    listed = OMITTED_LISTED
    payload, hidden_total = assemble(listed)
    while len(dumps(payload)) > max_chars:
        if chosen:
            omitted.append(chosen.pop()[0])  # the lowest priority chosen so far
        elif listed:
            listed //= 2  # nothing left to drop: list fewer of the omitted objects (their number stays)
        else:
            break
        payload, hidden_total = assemble(listed)
    text = dumps(payload)
    included_count = sum(len(s["objects"]) for s in payload["schemas"])
    status = _status(fresh, len(selected))
    if selected and not chosen and status == "ok":
        status = "budget_too_small"  # something is selected, but not even one object fits the budget
    usable = status == "ok"
    gone = [
        {"kind": i["kind"], "schema": i["schema"], "object": i["object"], "column": i["column"]}
        for i, ok in unavailable
        if not ok
    ]
    return {
        "format": FORMAT,
        "status": status,
        "usable": usable,
        "complete": not omitted,
        "provider": source.provider,
        "snapshot": source.snapshot,
        "freshness": fresh,
        "selection": {
            "revision": revision,
            "selected_objects": len(selected),
            "included_objects": included_count,
            "unavailable_count": len(gone),
            "unavailable": gone[:UNAVAILABLE_LISTED],
        },
        "budget": {
            "max_chars": max_chars,
            "used_chars": len(text),
            "context_fingerprint": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        },
        "context": payload,
    }


def _order_key(key):
    return (_order(key[0]), _order(key[1]))
