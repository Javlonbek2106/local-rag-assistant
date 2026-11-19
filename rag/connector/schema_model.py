"""The canonical form of a discovered schema, and its fingerprint (roadmap M4.2). Pure: no database, no network.

A fingerprint answers "is this the same structure?". Two discoveries of the same structure give the same fingerprint
whatever order the external server listed things in, and a change of anything that matters changes it.

What goes in (version 1): one JSON record per line, in this order of kinds:

    ["o", schema, object, type]                                              a table, view or materialized view
    ["c", schema, object, column, data_type, nullable]                       a column
    ["k", schema, object, kind, name, [columns], ref_schema, ref_object, [ref_columns]]   a primary/unique/foreign key
    ["i", schema, object, name, [columns], unique, primary]                  an index

Records of one kind are sorted by their identity (the leading fields) as UTF-8 BYTES, so the order does not depend on
any collation of any server (the database side sorts with COLLATE "C", which is the same order). The key columns inside
a record keep their order: it is part of the definition.

What is left out on purpose, because it is not structure or because it is not stable:
- the ordinal position of a column (reordering columns changes nothing a query can see by name);
- the NAME of a primary key and of the index that backs it (engines make up names such as
  PK__customer__3213E83FFECE83F9; the same table made again would otherwise look changed);
- the host, the connection, the database name and every time: they identify WHERE and WHEN, not WHAT.
The provider is part of it (a type called `int` in two engines is not the same promise).

The record is JSON with every non-ASCII character escaped, so the bytes hashed are the same on every platform.
"""

import hashlib
import json

FINGERPRINT_VERSION = 1
KINDS = ("o", "c", "k", "i")


def _text(value):
    return value if value is not None else ""


def object_record(schema, name, object_type):
    return ["o", schema, name, object_type]


def column_record(schema, object_name, column, data_type, nullable):
    return ["c", schema, object_name, column, data_type, bool(nullable)]


def constraint_identity(kind, name):
    """A primary key is one per table: it has no identity beyond that. Others are told apart by their name."""
    return "" if kind == "primary_key" else name


def constraint_record(schema, object_name, kind, name, columns, ref_schema=None, ref_object=None, ref_columns=None):
    return [
        "k",
        schema,
        object_name,
        kind,
        constraint_identity(kind, name),
        list(columns),
        _text(ref_schema),
        _text(ref_object),
        list(ref_columns or []),
    ]


def index_record(schema, object_name, name, columns, unique, primary):
    return ["i", schema, object_name, "" if primary else name, list(columns), bool(unique), bool(primary)]


def _bytes(value):
    return value.encode("utf-8")


# The leading fields that identify a record of each kind (the sort key).
IDENTITY = {"o": 3, "c": 4, "k": 5, "i": 4}


def sort_key(record):
    return tuple(_bytes(field) for field in record[1 : IDENTITY[record[0]]])


class Fingerprinter:
    """Feeds records, in canonical order, to SHA-256. The database side streams them already sorted; `fingerprint_of`
    sorts them itself (for tests, and for any caller that has a catalogue in memory)."""

    def __init__(self, provider):
        self._hash = hashlib.sha256()
        self._last = None
        self._write(["schema-fingerprint", FINGERPRINT_VERSION, provider])

    def _write(self, record):
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=True)
        self._hash.update(line.encode("ascii") + b"\n")

    def add(self, record):
        """Records must arrive in canonical order: kinds in KINDS order, each kind sorted by `sort_key`."""
        order = (KINDS.index(record[0]), sort_key(record))
        if self._last is not None and order < self._last:
            raise ValueError("records are not in canonical order")
        self._last = order
        self._write(record)

    def hexdigest(self):
        return self._hash.hexdigest()


def fingerprint_of(provider, records):
    """The fingerprint of any collection of records, in any order."""
    ordered = sorted(records, key=lambda r: (KINDS.index(r[0]), sort_key(r)))
    fingerprinter = Fingerprinter(provider)
    for record in ordered:
        fingerprinter.add(record)
    return fingerprinter.hexdigest()


def records_of(objects, columns, constraints, indexes):
    """Canonical records from plain rows: objects (schema, name, type), columns (schema, object, ordinal, name, type,
    nullable), constraints (schema, object, kind, name, columns, ref_schema, ref_object, ref_columns), indexes (schema,
    object, name, columns, unique, primary)."""
    out = [object_record(s, n, t) for s, n, t in objects]
    out += [column_record(s, o, c, t, nl) for s, o, _ordinal, c, t, nl in columns]
    out += [constraint_record(*row) for row in constraints]
    out += [index_record(*row) for row in indexes]
    return out
