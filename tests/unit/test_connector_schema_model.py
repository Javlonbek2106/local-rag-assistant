"""M4.2: the canonical form and the fingerprint of a discovered schema. Equivalent structures give the same fingerprint
whatever the order they were listed in; any change that matters gives another."""

import random

import pytest

from rag.connector import schema_model as sm

OBJECTS = [("sales", "customer", "table"), ("sales", "order_line", "table"), ("hr", "big", "view")]
COLUMNS = [
    ("sales", "customer", 1, "id", "integer", False),
    ("sales", "customer", 2, "name", "text", True),
    ("sales", "order_line", 1, "order_id", "integer", False),
    ("sales", "order_line", 2, "customer_id", "integer", False),
    ("hr", "big", 1, "x", "integer", True),
]
CONSTRAINTS = [
    ("sales", "customer", "primary_key", "pk_customer", ["id"], None, None, None),
    ("sales", "order_line", "primary_key", "pk_ol", ["order_id", "customer_id"], None, None, None),
    ("sales", "order_line", "foreign_key", "fk_customer", ["customer_id"], "sales", "customer", ["id"]),
]
INDEXES = [
    ("sales", "customer", "pk_customer", ["id"], True, True),
    ("sales", "order_line", "ix_c", ["customer_id"], False, False),
]


def fp(objects=OBJECTS, columns=COLUMNS, constraints=CONSTRAINTS, indexes=INDEXES, provider="postgresql"):
    return sm.fingerprint_of(provider, sm.records_of(objects, columns, constraints, indexes))


def changed(items, index, new):
    out = list(items)
    out[index] = new
    return out


# ---------- determinism ----------


def test_the_fingerprint_is_a_sha256_hex_and_is_stable():
    first = fp()
    assert len(first) == 64 and int(first, 16) >= 0 and fp() == first


def test_the_order_things_were_listed_in_does_not_matter():
    reference = fp()
    rng = random.Random(7)
    for _ in range(20):
        shuffled = [list(group) for group in (OBJECTS, COLUMNS, CONSTRAINTS, INDEXES)]
        for group in shuffled:
            rng.shuffle(group)
        assert fp(*shuffled) == reference


def test_a_fixed_catalogue_has_a_fixed_fingerprint_so_the_algorithm_cannot_change_unnoticed():
    import hashlib
    import json

    tiny = sm.fingerprint_of(
        "postgresql",
        sm.records_of(
            [("s", "t", "table")],
            [("s", "t", 1, "id", "integer", False)],
            [("s", "t", "primary_key", "pk", ["id"], None, None, None)],
            [],
        ),
    )
    lines = [
        ["schema-fingerprint", 1, "postgresql"],
        ["o", "s", "t", "table"],
        ["c", "s", "t", "id", "integer", False],
        ["k", "s", "t", "primary_key", "", ["id"], "", "", []],
    ]
    text = "".join(json.dumps(line, separators=(",", ":")) + "\n" for line in lines)
    assert tiny == hashlib.sha256(text.encode()).hexdigest()  # the exact bytes that are hashed, written out


def test_the_comparison_of_names_is_by_bytes_not_by_any_collation():
    objects = [("s", "Zebra", "table"), ("s", "apple", "table"), ("s", "éclair", "table"), ("s", "Éclair", "table")]
    ordered = sorted(sm.records_of(objects, [], [], []), key=lambda r: (sm.KINDS.index(r[0]), sm.sort_key(r)))
    assert [r[2] for r in ordered] == [
        "Zebra",
        "apple",
        "Éclair",
        "éclair",
    ]  # uppercase before lowercase, then UTF-8 bytes
    assert fp(objects=objects, columns=[], constraints=[], indexes=[]) == fp(
        objects=list(reversed(objects)), columns=[], constraints=[], indexes=[]
    )


def test_non_ascii_names_hash_the_same_everywhere():
    a = fp(objects=[("s", "таблица", "table")], columns=[], constraints=[], indexes=[])
    assert a == fp(objects=[("s", "таблица", "table")], columns=[], constraints=[], indexes=[]) and len(a) == 64


def test_records_must_arrive_in_canonical_order_when_streamed():
    f = sm.Fingerprinter("postgresql")
    f.add(sm.object_record("a", "b", "table"))
    with pytest.raises(ValueError):
        f.add(sm.object_record("a", "a", "table"))  # the same kind, an earlier key
    g = sm.Fingerprinter("postgresql")
    g.add(sm.column_record("a", "b", "c", "int", True))
    with pytest.raises(ValueError):
        g.add(sm.object_record("z", "z", "table"))  # a kind that comes before columns


# ---------- what changes it ----------


@pytest.mark.parametrize(
    "mutation",
    [
        lambda: fp(provider="mssql"),
        lambda: fp(objects=changed(OBJECTS, 2, ("hr", "big", "table"))),  # a view became a table
        lambda: fp(objects=OBJECTS + [("sales", "new", "table")]),
        lambda: fp(objects=OBJECTS[:2]),
        lambda: fp(objects=changed(OBJECTS, 0, ("sales", "customers", "table"))),  # a rename is a different structure
        lambda: fp(columns=changed(COLUMNS, 1, ("sales", "customer", 2, "name", "varchar(50)", True))),
        lambda: fp(columns=changed(COLUMNS, 1, ("sales", "customer", 2, "name", "text", False))),
        lambda: fp(columns=COLUMNS + [("sales", "customer", 3, "email", "text", True)]),
        lambda: fp(columns=COLUMNS[:-1]),
        lambda: fp(constraints=CONSTRAINTS[:-1]),  # the foreign key is gone
        lambda: fp(
            constraints=changed(
                CONSTRAINTS,
                1,
                ("sales", "order_line", "primary_key", "pk_ol", ["customer_id", "order_id"], None, None, None),
            )
        ),
        lambda: fp(
            constraints=changed(
                CONSTRAINTS,
                2,
                ("sales", "order_line", "foreign_key", "fk_other", ["customer_id"], "sales", "customer", ["id"]),
            )
        ),
        lambda: fp(
            constraints=changed(
                CONSTRAINTS,
                2,
                ("sales", "order_line", "foreign_key", "fk_customer", ["customer_id"], "hr", "customer", ["id"]),
            )
        ),
        lambda: fp(indexes=INDEXES[:1]),
        lambda: fp(indexes=changed(INDEXES, 1, ("sales", "order_line", "ix_c", ["customer_id"], True, False))),
        lambda: fp(indexes=changed(INDEXES, 1, ("sales", "order_line", "ix_c", ["order_id"], False, False))),
    ],
)
def test_anything_that_matters_changes_the_fingerprint(mutation):
    assert mutation() != fp()


def test_what_is_not_structure_does_not_change_it():
    reordered = [(s, o, 10 - n, c, t, nl) for s, o, n, c, t, nl in COLUMNS]  # the ordinal position is not part of it
    assert fp(columns=reordered) == fp()
    renamed_pk = changed(
        CONSTRAINTS, 0, ("sales", "customer", "primary_key", "PK__customer__3213E83FFECE83F9", ["id"], None, None, None)
    )
    assert fp(constraints=renamed_pk) == fp()  # engines invent primary key names
    renamed_index = changed(INDEXES, 0, ("sales", "customer", "PK__x", ["id"], True, True))
    assert fp(indexes=renamed_index) == fp()  # and the name of the index behind it


def test_a_boolean_is_a_boolean_and_columns_of_a_key_keep_their_order():
    assert sm.column_record("s", "t", "c", "int", 1) == sm.column_record("s", "t", "c", "int", True)
    assert sm.constraint_record("s", "t", "unique", "u", ["a", "b"]) != sm.constraint_record(
        "s", "t", "unique", "u", ["b", "a"]
    )


def test_a_foreign_key_and_its_name_are_part_of_it_but_the_name_of_a_primary_key_is_not():
    assert sm.constraint_identity("primary_key", "anything") == ""
    assert sm.constraint_identity("foreign_key", "fk") == "fk" and sm.constraint_identity("unique", "u") == "u"


def test_an_empty_schema_has_a_fingerprint_too_and_it_depends_on_the_provider():
    assert fp([], [], [], []) != fp([], [], [], [], provider="mssql")
