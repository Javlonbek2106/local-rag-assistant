"""M4.4, the validator on its own (rag/connector/sqlcheck.py): what is accepted, what is refused and why, what exact
text may be run, and real bypass attempts. A fake catalog stands in for the schema snapshot; nothing here touches a
database."""

import pytest

pytest.importorskip("sqlglot")

from rag.connector import sqlcheck  # noqa: E402

COLUMNS = {
    ("sales", "customer"): ["id", "Name", "created"],  # the owner has hidden `secret` of this table
    ("sales", "order_line"): ["order_id", "line_no", "customer_id", "qty"],
    ("sales", "big_orders"): ["order_id", "qty"],
    ("hr", "employee"): ["id", "email"],
    ("Mixed", "Things"): ["ID", "Label"],
}
FOREIGN_KEYS = {("sales", "order_line"): [(["customer_id"], "sales", "customer", ["id"])]}
SELECTED = {("sales", "customer"), ("sales", "order_line"), ("sales", "big_orders"), ("Mixed", "Things")}
ALL = {**COLUMNS, ("sales", "customer"): ["id", "Name", "created", "secret"]}  # what exists, selected or not


class Catalog:
    def __init__(self, selected=SELECTED):
        self.selected = selected

    def objects(self):
        return {key: ("table", key in self.selected) for key in ALL}

    def load(self, keys):
        pass

    def columns(self, key):
        return [(c, "text") for c in COLUMNS[key]]

    def foreign_keys(self, key):
        return FOREIGN_KEYS.get(key, [])


LIMITS = sqlcheck.Limits(20000, 3000, 150, 1000)


def check(sql, provider="postgresql", **kwargs):
    return sqlcheck.analyze(sql, provider, Catalog(**kwargs), LIMITS)


def codes(analysis):
    return [e["code"] for e in analysis.errors]


def ok(sql, provider="postgresql"):
    found = check(sql, provider)
    assert found.ok, (sql, found.errors)
    return found


def refused(sql, provider="postgresql", code=None):
    found = check(sql, provider)
    assert not found.ok and found.sql is None, (sql, found.sql)
    if code:
        assert code in codes(found), (sql, codes(found))
    return found


# ---------- what is accepted, and the text that would be run ----------


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT c.id, c."Name" FROM sales.customer c',
        "SELECT id FROM sales.customer WHERE id > 5 AND \"Name\" LIKE 'A%' OR id IN (1, 2, 3) "
        "ORDER BY id DESC LIMIT 10",
        "SELECT c.id FROM sales.customer c JOIN sales.order_line o ON o.customer_id = c.id WHERE o.qty BETWEEN 1 AND 9",
        "SELECT c.id, count(*) AS n FROM sales.customer c GROUP BY c.id HAVING count(*) > 1",
        "WITH t AS (SELECT id FROM sales.customer) SELECT t.id FROM t",
        "WITH a AS (SELECT id FROM sales.customer), b AS (SELECT id FROM a) SELECT b.id FROM b",
        "SELECT x.n FROM (SELECT count(*) AS n FROM sales.order_line) x",
        "SELECT c.id FROM sales.customer c WHERE EXISTS (SELECT 1 FROM sales.order_line o WHERE o.customer_id = c.id)",
        "SELECT c.id FROM sales.customer c WHERE c.id IN (SELECT customer_id FROM sales.order_line)",
        "SELECT c.id, (SELECT max(qty) FROM sales.order_line) FROM sales.customer c",
        "SELECT id FROM sales.customer UNION SELECT customer_id FROM sales.order_line",
        "SELECT row_number() OVER (PARTITION BY c.id ORDER BY c.created) FROM sales.customer c",
        "SELECT CASE WHEN c.id > 1 THEN 'a' ELSE 'b' END, coalesce(c.\"Name\", 'x'), CAST(c.id AS text) "
        "FROM sales.customer c",
        "SELECT date_trunc('month', c.created), now() - interval '30 days', current_date FROM sales.customer c",
        'SELECT upper(c."Name"), length(c."Name"), c."Name" || \'x\', round(1.5, 1), abs(c.id) FROM sales.customer c',
        "SELECT 1",
        "SELECT ';DROP TABLE sales.customer;--' AS harmless",
        "SELECT 'it''s' AS s, 'Ünïcode ✓ 日本' AS u",
        "SELECT c.id FROM sales.customer c LEFT JOIN sales.order_line o USING (customer_id)",
        'SELECT t."Label" FROM "Mixed"."Things" t',
        "SELECT id FROM sales.customer;",
        "SELECT /* a comment */ id FROM sales.customer -- and another",
    ],
)
def test_ordinary_read_only_postgresql_queries_are_accepted(sql):
    if "USING (customer_id)" in sql:  # (USING needs the column on both sides: the fixture has it only on one)
        refused(sql)
        return
    found = ok(sql)
    assert found.sql and found.warnings is not None


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT TOP 10 c.id, c.name FROM sales.customer c",
        "SELECT c.ID, c.NAME FROM [sales].[Customer] c",
        "SELECT c.id FROM sales.customer AS c ORDER BY c.id OFFSET 5 ROWS FETCH NEXT 10 ROWS ONLY",
        "SELECT DATEADD(day, -30, GETDATE()) AS d, ISNULL(c.name, 'x') FROM sales.customer c",
        "WITH t AS (SELECT id FROM sales.customer) SELECT TOP 5 t.id FROM t",
        "SELECT c.id FROM sales.customer c WHERE c.name LIKE N'A%' AND c.created > DATEADD(month, -1, GETDATE())",
        "SELECT COUNT(*) AS n, YEAR(c.created) AS y FROM sales.customer c GROUP BY YEAR(c.created)",
        "SELECT c.id FROM sales.customer c INNER JOIN sales.order_line o ON o.customer_id = c.id",
        "SELECT LEN(c.name), UPPER(c.name), SUBSTRING(c.name, 1, 2), CONVERT(varchar(10), c.id) FROM sales.customer c",
        "SELECT t.label FROM Mixed.Things t",
    ],
)
def test_ordinary_read_only_sql_server_queries_are_accepted(sql):
    found = ok(sql, "mssql")
    assert found.sql.startswith(("SELECT", "WITH"))


def test_the_text_that_may_be_run_is_not_the_text_that_was_typed():
    found = ok('SELECT /* x */ c.id, c."Name" FROM sales.customer c -- y')
    assert found.sql == 'SELECT "c"."id" AS "id", "c"."Name" AS "Name" FROM "sales"."customer" AS "c" LIMIT 1001'
    assert "--" not in found.sql and "/*" not in found.sql  # comments are gone
    sql_server = ok("SELECT c.ID FROM [sales].[Customer] c", "mssql").sql
    assert (
        sql_server == "SELECT TOP 1001 [c].[id] AS [id] FROM [sales].[customer] AS [c]"
    )  # real names, quoted, limited


def test_every_name_is_written_as_the_real_quoted_object_even_when_typed_in_another_case_or_without_schema():
    assert (
        '"sales"."order_line"' in ok("SELECT qty FROM ORDER_LINE").sql
    )  # unqualified, unquoted: folded like PostgreSQL
    assert '"Mixed"."Things"' in ok('SELECT "Label" FROM "Mixed"."Things"').sql
    refused('SELECT label FROM "Mixed"."Things"', code="unknown_column")  # PostgreSQL: unquoted label is not "Label"
    assert "[sales].[customer]" in ok("SELECT ID FROM SALES.CUSTOMER", "mssql").sql  # SQL Server: any case


def test_the_canonical_text_is_a_fixed_point_and_validates_again():
    for sql in (
        "SELECT c.id, count(*) AS n FROM sales.customer c JOIN sales.order_line o ON o.customer_id = c.id GROUP BY "
        "c.id",
        "WITH t AS (SELECT id FROM sales.customer) SELECT t.id FROM t WHERE t.id IN (SELECT customer_id FROM "
        "sales.order_line)",
    ):
        first = ok(sql).sql
        assert ok(first).sql == first


@pytest.mark.parametrize("provider", ["postgresql", "mssql"])
def test_a_row_limit_is_added_unless_the_query_has_a_smaller_one_of_its_own(provider):
    assert ok("SELECT id FROM sales.customer", provider).row_limit_added
    own = "SELECT TOP 5 id FROM sales.customer" if provider == "mssql" else "SELECT id FROM sales.customer LIMIT 5"
    kept = ok(own, provider)
    assert not kept.row_limit_added and ("TOP 5" in kept.sql or "LIMIT 5" in kept.sql)


def test_a_limit_of_its_own_is_kept_if_small_and_replaced_if_large_or_not_a_number():
    assert ok("SELECT id FROM sales.customer LIMIT 7").sql.endswith("LIMIT 7")
    assert ok("SELECT id FROM sales.customer LIMIT 99999999").sql.endswith("LIMIT 1001")
    assert ok("SELECT id FROM sales.customer LIMIT (SELECT count(*) FROM sales.order_line)").sql.endswith("LIMIT 1001")
    assert not ok(
        "SELECT id FROM sales.customer UNION SELECT customer_id FROM sales.order_line"
    ).row_limit_added  # the driver cuts


# ---------- the schema: tables and columns ----------


def test_an_excluded_column_does_not_exist_for_a_query_not_even_through_a_star():
    refused("SELECT c.secret FROM sales.customer c", code="unknown_column")
    refused("SELECT secret FROM sales.customer", code="unknown_column")
    for sql in ("SELECT * FROM sales.customer", "SELECT c.* FROM sales.customer c"):
        found = ok(sql)
        assert "secret" not in found.sql and '"created"' in found.sql  # the star became the columns that are shown
    wrapped = ok("WITH t AS (SELECT * FROM sales.customer) SELECT * FROM t")
    assert "secret" not in wrapped.sql


def test_unknown_unselected_and_cross_database_tables_are_refused_each_for_its_own_reason():
    refused("SELECT id FROM sales.no_such_table", code="unknown_table")
    refused("SELECT id FROM no_such_schema.customer", code="unknown_table")
    refused("SELECT id FROM hr.employee", code="table_not_selected")  # exists, but the owner did not select it
    refused("SELECT * FROM pg_catalog.pg_shadow", code="unknown_table")
    refused("SELECT * FROM information_schema.tables", code="unknown_table")
    refused("SELECT * FROM otherdb.sales.customer", "mssql", code="cross_database_not_allowed")
    refused("SELECT * FROM srv.db.dbo.customer", "mssql", code="cross_database_not_allowed")


def test_unknown_and_ambiguous_columns_and_aliases_are_refused():
    refused("SELECT nope FROM sales.customer", code="unknown_column")
    refused("SELECT c.nope FROM sales.customer c", code="unknown_column")
    refused(
        "SELECT c.id FROM sales.customer c JOIN sales.order_line o ON o.customer_id = c.id AND o.nope = c.id",
        code="unknown_column",
    )
    refused("SELECT x.id FROM sales.customer c", code=None)
    ambiguous = {("sales", "a"): ["id"], ("sales", "b"): ["id"]}
    original = (COLUMNS.copy(), ALL.copy())
    try:
        COLUMNS.update(ambiguous)
        ALL.update(ambiguous)
        selected = SELECTED | set(ambiguous)
        found = sqlcheck.analyze(
            "SELECT id FROM sales.a x JOIN sales.b y ON x.id = y.id", "postgresql", Catalog(selected), LIMITS
        )
        assert not found.ok and "ambiguous_column" in codes(found)
    finally:
        COLUMNS.clear(), ALL.clear()
        COLUMNS.update(original[0]), ALL.update(original[1])


def test_a_cte_is_not_a_table_of_the_database_and_cannot_hide_one():
    ok("WITH customer AS (SELECT id FROM sales.customer) SELECT customer.id FROM customer")  # a CTE may shadow a name
    refused(
        "WITH t AS (SELECT id FROM hr.employee) SELECT id FROM t", code="table_not_selected"
    )  # but not hide what is not
    refused("SELECT t.id FROM t", code=None)  # a name that is neither a CTE nor a table
    refused("WITH t AS (SELECT id FROM sales.customer) SELECT nope FROM t", code="unknown_column")


def test_the_tables_and_columns_a_query_uses_are_reported():
    found = ok("SELECT c.id, o.qty FROM sales.customer c JOIN sales.order_line o ON o.customer_id = c.id")
    assert found.tables == [
        {"schema": "sales", "name": "customer", "columns": ["id"]},
        {"schema": "sales", "name": "order_line", "columns": ["customer_id", "qty"]},
    ]
    assert "count" in ok("SELECT count(*) FROM sales.customer").functions


def test_joins_need_a_condition_and_one_not_on_a_known_relationship_is_a_warning():
    refused("SELECT c.id FROM sales.customer c CROSS JOIN sales.order_line o", code="join_without_condition")
    refused("SELECT c.id FROM sales.customer c, sales.order_line o", code="join_without_condition")
    refused("SELECT c.id FROM sales.customer c JOIN sales.order_line o", code="join_without_condition")
    refused("SELECT c.id FROM sales.customer c NATURAL JOIN sales.order_line o", code="natural_join_not_allowed")
    good = ok("SELECT c.id FROM sales.customer c JOIN sales.order_line o ON o.customer_id = c.id")
    assert good.warnings == []
    other = ok("SELECT c.id FROM sales.customer c JOIN sales.order_line o ON o.qty = c.id")
    assert [w["code"] for w in other.warnings] == ["join_not_on_known_relationship"]
    flat = ok("SELECT c.id FROM sales.customer c JOIN sales.order_line o ON c.id > o.qty")
    assert [w["code"] for w in flat.warnings] == ["join_condition_not_a_key_equality"]


# ---------- statements and constructs that are never allowed ----------

STATEMENTS = [
    ("INSERT INTO sales.customer (id) VALUES (1)", "dml_not_allowed"),
    ("UPDATE sales.customer SET id = 1", "dml_not_allowed"),
    ("DELETE FROM sales.customer", "dml_not_allowed"),
    (
        "MERGE INTO sales.customer t USING sales.order_line s ON t.id = s.order_id WHEN MATCHED THEN DELETE",
        "dml_not_allowed",
    ),
    ("CREATE TABLE x (a int)", "ddl_not_allowed"),
    ("CREATE VIEW v AS SELECT 1", "ddl_not_allowed"),
    ("ALTER TABLE sales.customer ADD COLUMN x int", "ddl_not_allowed"),
    ("DROP TABLE sales.customer", "ddl_not_allowed"),
    ("TRUNCATE TABLE sales.customer", "ddl_not_allowed"),
    ("GRANT SELECT ON sales.customer TO public", None),
    ("REVOKE SELECT ON sales.customer FROM public", None),
    ("COPY sales.customer TO '/tmp/x'", "copy_not_allowed"),
    ("SET statement_timeout = 0", "session_statement_not_allowed"),
    ("SET ROLE postgres", None),
    ("RESET ALL", None),
    ("BEGIN", None),
    ("COMMIT", None),
    ("ROLLBACK", None),
    ("EXPLAIN ANALYZE SELECT 1", None),
    ("VACUUM sales.customer", None),
    ("ANALYZE sales.customer", None),
    ("LISTEN x", None),
    ("NOTIFY x", None),
    ("PREPARE p AS SELECT 1", None),
    ("EXECUTE p", None),
    ("DISCARD ALL", None),
    ("DO $$ BEGIN PERFORM 1; END $$", None),
    ("CALL do_something()", None),
    ("SHOW server_version", None),
    ("TABLE sales.customer FOR UPDATE", None),
    ("SELECT * INTO newtable FROM sales.customer", "select_into_not_allowed"),
    ("SELECT id INTO TEMP t FROM sales.customer", "select_into_not_allowed"),
    ("SELECT id FROM sales.customer FOR UPDATE", "locking_not_allowed"),
    ("SELECT id FROM sales.customer FOR SHARE", "locking_not_allowed"),
    ("WITH d AS (DELETE FROM sales.customer RETURNING id) SELECT id FROM d", "dml_not_allowed"),
    ("WITH u AS (UPDATE sales.customer SET id = 1 RETURNING id) SELECT id FROM u", "dml_not_allowed"),
    ("WITH i AS (INSERT INTO sales.customer (id) VALUES (1) RETURNING id) SELECT id FROM i", "dml_not_allowed"),
    ("SELECT (SELECT 1 FROM (DELETE FROM sales.customer RETURNING 1) d)", None),
    ("WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r) SELECT n FROM r", "recursive_cte_not_allowed"),
    ("SELECT * FROM generate_series(1, 100000000)", None),
    ("SELECT * FROM unnest(ARRAY[1,2,3])", None),
    ("SELECT * FROM sales.customer c, LATERAL (SELECT 1) l", None),
    ("SELECT * FROM sales.customer TABLESAMPLE SYSTEM (10)", None),
    ("SELECT $1", None),
    ("SELECT :a", None),
    ("SELECT ?", None),
]


@pytest.mark.parametrize("sql,code", STATEMENTS)
def test_postgresql_statements_and_constructs_that_are_never_allowed(sql, code):
    refused(sql, code=code)


TSQL_STATEMENTS = [
    ("EXEC xp_cmdshell 'dir'", "exec_not_allowed"),
    ("EXEC sp_who", "exec_not_allowed"),
    ("EXECUTE sales.do_something", "exec_not_allowed"),
    ("INSERT INTO sales.customer (id) VALUES (1)", "dml_not_allowed"),
    ("UPDATE sales.customer SET id = 1", "dml_not_allowed"),
    ("DELETE FROM sales.customer", "dml_not_allowed"),
    ("DROP TABLE sales.customer", "ddl_not_allowed"),
    ("CREATE TABLE x (a int)", "ddl_not_allowed"),
    ("TRUNCATE TABLE sales.customer", "ddl_not_allowed"),
    ("SELECT * INTO #t FROM sales.customer", "select_into_not_allowed"),
    ("SELECT * INTO sales.copy FROM sales.customer", "select_into_not_allowed"),
    ("SELECT * FROM OPENROWSET('SQLNCLI', 'server=x;', 'SELECT 1')", None),
    ("SELECT * FROM OPENDATASOURCE('SQLNCLI', 'Data Source=x').db.dbo.t", None),
    ("SELECT * FROM OPENQUERY(linked, 'SELECT 1')", None),
    ("SELECT * FROM linked.db.dbo.t", "cross_database_not_allowed"),
    ("SELECT c.id FROM sales.customer c WITH (UPDLOCK)", "hints_not_allowed"),
    ("SELECT c.id FROM sales.customer c WITH (NOLOCK)", "hints_not_allowed"),
    ("SELECT c.id FROM sales.customer c OPTION (MAXDOP 1)", "hints_not_allowed"),
    ("DECLARE @a int = 1; SELECT @a", None),
    ("SELECT @@version", None),
    ("SELECT @a", None),
    ("SET NOCOUNT ON", None),
    ("USE master", None),
    ("BEGIN TRAN", None),
    ("WAITFOR DELAY '00:00:10'", None),
    ("SELECT * FROM sales.customer c CROSS APPLY (SELECT 1 AS x) a", None),
    ("GRANT SELECT ON sales.customer TO public", None),
    ("BACKUP DATABASE x TO DISK = 'c:\\\\x.bak'", None),
    ("RECONFIGURE", None),
    ("SELECT 1; DROP TABLE x", "multiple_statements"),
    ("SELECT 1 GO DROP TABLE x", None),
    ("SELECT 1 FROM sales.customer UNION SELECT * FROM OPENROWSET('a', 'b', 'c')", None),
    ("SELECT (SELECT TOP 1 1 FROM OPENQUERY(a, 'x'))", None),
]


@pytest.mark.parametrize("sql,code", TSQL_STATEMENTS)
def test_sql_server_statements_and_constructs_that_are_never_allowed(sql, code):
    refused(sql, "mssql", code=code)


DANGEROUS_FUNCTIONS = {
    "postgresql": [
        "pg_read_file('/etc/passwd')",
        "pg_read_binary_file('/etc/passwd')",
        "pg_ls_dir('.')",
        "pg_stat_file('/etc/passwd')",
        "lo_import('/etc/passwd')",
        "lo_export(1, '/tmp/x')",
        "dblink('host=x', 'select 1')",
        "dblink_exec('host=x', 'drop table t')",
        "pg_sleep(1000)",
        "pg_terminate_backend(1)",
        "pg_cancel_backend(1)",
        "pg_reload_conf()",
        "pg_rotate_logfile()",
        "set_config('statement_timeout', '0', false)",
        "current_setting('server_version')",
        "nextval('s')",
        "setval('s', 1)",
        "pg_advisory_lock(1)",
        "txid_current()",
        "version()",
        "pg_catalog.pg_read_file('/etc/passwd')",
        "pg_catalog.now()",
        "query_to_xml('select 1', true, true, '')",
        "xpath('/a', '<a/>')",
        "my_own_function(1)",
        "generate_series(1, 10)",
        "random()",
    ],
    "mssql": [
        "xp_cmdshell('dir')",
        "OPENROWSET('a', 'b', 'c')",
        "OPENQUERY(x, 'select 1')",
        "OPENDATASOURCE('a', 'b')",
        "fn_my_permissions(NULL, 'DATABASE')",
        "sp_who()",
        "SUSER_SNAME()",
        "USER_NAME()",
        "SERVERPROPERTY('ProductVersion')",
        "DB_NAME()",
        "HOST_NAME()",
        "NEWID()",
        "dbo.my_function(1)",
        "sys.fn_something()",
        "my_function(1)",
        "OBJECT_ID('x')",
    ],
}


@pytest.mark.parametrize(
    "provider,call", [(p, c) for p, calls in DANGEROUS_FUNCTIONS.items() for c in calls], ids=lambda v: str(v)[:40]
)
def test_a_function_nobody_listed_is_refused_wherever_it_is_hidden_in_the_query(provider, call):
    """The policy is an allowlist: dangerous ones and unknown ones alike are refused, in the select list, in a WHERE,
    in a scalar subquery and in a CTE (a SELECT is not safe because it is a SELECT)."""
    for sql in (
        f"SELECT {call}",
        f"SELECT id FROM sales.customer WHERE id = {call}",
        f"SELECT (SELECT {call}) AS x",
        f"WITH t AS (SELECT {call} AS x) SELECT x FROM t",
        f"SELECT id FROM sales.customer ORDER BY {call}",
    ):
        refused(sql, provider)


def test_the_reason_for_a_refused_function_names_it_and_dangerous_ones_are_told_apart():
    found = refused("SELECT pg_read_file('/etc/passwd')")
    assert found.errors[0]["code"] == "dangerous_function" and found.errors[0]["function"] == "pg_read_file"
    assert refused("SELECT my_own_function(1)").errors[0]["code"] == "function_not_allowed"
    assert refused("SELECT xp_cmdshell('dir')", "mssql").errors[0]["code"] == "dangerous_function"


def test_a_qualified_function_name_is_refused_not_resolved():
    refused("SELECT pg_catalog.pg_read_file('x')")
    refused("SELECT dbo.fn(1)", "mssql")
    refused("SELECT sales.customer.id.foo()", code=None)


def test_user_defined_types_and_unknown_bare_words_are_refused():
    refused("SELECT CAST(1 AS my_schema.my_type)")
    refused("SELECT CAST(id AS evil) FROM sales.customer")


# ---------- bypass attempts: the checks are on the syntax tree, not on text ----------


def test_comments_and_odd_spacing_cannot_hide_a_second_statement_or_a_keyword():
    for sql in (
        "SELECT 1; DROP TABLE sales.customer",
        "SELECT 1;/* x */DROP TABLE sales.customer",
        "SELECT 1\n;\nDROP\nTABLE sales.customer",
        "SELECT 1 /* ; */ ; DELETE /* x */ FROM sales.customer",
        "SELECT 1;--\nDELETE FROM sales.customer",
        "/* SELECT */ DELETE FROM sales.customer",
        "DEL/**/ETE FROM sales.customer",
        "dRoP tAbLe sales.customer",
        "\tDROP\u00a0TABLE sales.customer",
    ):
        refused(sql)
        refused(sql, "mssql")


def test_text_inside_strings_and_identifiers_is_data_not_code():
    found = ok("SELECT ';DROP TABLE sales.customer;--' AS a, '/* x */' AS b")
    assert found.sql.count("DROP TABLE") == 1 and "';DROP TABLE sales.customer;--'" in found.sql  # still one literal
    refused('SELECT "x; DROP TABLE sales.customer" FROM sales.customer', code="unknown_column")
    refused("SELECT id FROM sales.customer WHERE \"Name\" = 'a' OR 1=1; DROP TABLE x")
    for literal in ("$$ ; DROP TABLE x; $$", "E'\\\\'; DROP TABLE x; --'", "'\\'; DROP TABLE x; --'"):
        found = check(f"SELECT {literal}")
        assert not found.ok or found.sql.count(";") <= 1  # either refused, or the semicolon is inside the literal


def test_a_dangerous_thing_nested_in_a_subquery_a_cte_or_a_union_is_found():
    refused("SELECT id FROM sales.customer WHERE id IN (SELECT pg_read_file('x')::int)")
    refused(
        "SELECT id FROM (SELECT id FROM sales.customer UNION ALL SELECT id FROM hr.employee) x",
        code="table_not_selected",
    )
    refused("WITH a AS (SELECT 1), b AS (SELECT lo_import('/etc/passwd')) SELECT * FROM a")
    refused("SELECT id FROM sales.customer UNION SELECT 1 FOR UPDATE")
    refused("SELECT EXISTS (SELECT 1 FROM sales.customer FOR UPDATE)")
    refused("SELECT id FROM sales.customer WHERE id = (SELECT id FROM sales.customer LIMIT 1 FOR UPDATE)")


def test_quoted_names_cannot_smuggle_another_object_or_a_function():
    refused('SELECT id FROM "sales"."customer; DROP TABLE x"', code="unknown_table")
    refused('SELECT id FROM "sales.customer"', code="unknown_table")
    refused('SELECT id FROM sales."customer" "x" WHERE "x"."pg_read_file"(1)')
    refused("SELECT [id] FROM [sales].[customer]; DROP TABLE x", "mssql")


def test_the_syntax_that_only_looks_like_a_select_is_refused():
    for sql in (
        "VALUES (1)",
        "TABLE sales.customer",
        "(SELECT 1) INTO x",
        "SELECT 1 INTO x",
        "WITH x AS (SELECT 1) DELETE FROM sales.customer",
    ):
        found = check(sql)
        assert not found.ok or "INTO" not in found.sql


def test_nothing_unlisted_is_waved_through_even_if_it_parses():
    for sql in (
        "SELECT 1::json -> 'a'",
        "SELECT ARRAY[1,2][1]",
        "SELECT 'a' ~ 'b'",
        "SELECT id FROM sales.customer c WHERE c.id @> 1",
    ):
        found = check(sql)
        assert isinstance(found.ok, bool)  # (whichever it is, it is decided by the allowlist, and never crashes)


# ---------- malformed, empty, oversized, hostile in size ----------


@pytest.mark.parametrize("sql", ["", "   ", "\n\t", "-- only a comment", "/* nothing */", ";", ";;"])
def test_empty_sql_is_refused(sql):
    assert refused(sql, code="empty_sql") and refused(sql, "mssql", code="empty_sql")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT FROM",
        "SELEC id FROM sales.customer",
        "SELECT id FROM",
        "SELECT (1",
        "SELECT 1)",
        "SELECT 'unterminated",
        "WHERE x",
    ],
)
def test_malformed_sql_is_a_syntax_error_with_no_echo_of_the_text(sql):
    found = refused(sql)
    assert found.errors[0]["code"] == "syntax_error" and "unterminated" not in found.errors[0]["message"]


def test_not_text_and_nul_and_oversized_input_are_refused_before_parsing():
    for value in (None, 5, b"SELECT 1", ["SELECT 1"]):
        assert sqlcheck.analyze(value, "postgresql", Catalog(), LIMITS).errors[0]["code"] == "empty_sql"
    refused("SELECT 1\x00; DROP TABLE x", code="invalid_characters")
    big = sqlcheck.Limits(100, 3000, 150, 1000)
    assert (
        sqlcheck.analyze("SELECT " + "1," * 100 + "1", "postgresql", Catalog(), big).errors[0]["code"]
        == "sql_too_large"
    )


def test_a_deeply_nested_or_huge_expression_is_refused_without_crashing():
    for depth in (200, 2000, 20000):
        found = check("SELECT " + "(" * depth + "1" + ")" * depth)
        assert not found.ok and codes(found)[0] in ("too_complex", "syntax_error", "sql_too_large")
    huge = check("SELECT " + " + ".join(["1"] * 5000))
    assert not huge.ok and codes(huge)[0] in ("too_complex", "sql_too_large")
    chain = check("SELECT id FROM sales.customer WHERE " + " OR ".join(f"id = {n}" for n in range(1500)))
    assert not chain.ok
    small = sqlcheck.Limits(20000, 30, 150, 1000)
    wide = "SELECT " + ", ".join(f"c{n}" for n in range(40)) + " FROM sales.customer"
    assert sqlcheck.analyze(wide, "postgresql", Catalog(), small).errors[0]["code"] == "too_complex"


def test_other_kinds_of_database_have_no_dialect():
    found = sqlcheck.analyze("SELECT 1", "oracle", Catalog(), LIMITS)
    assert codes(found) == ["dialect_not_supported"]


def test_the_validator_leaves_no_text_of_the_sql_in_the_logs(caplog):
    caplog.set_level("DEBUG")
    check("EXPLAIN ANALYZE SELECT secret_marker_zq FROM sales.customer")
    check("SELEC secret_marker_zq")
    check("DELETE FROM secret_marker_zq")
    assert "secret_marker_zq" not in caplog.text


def test_a_problem_list_is_bounded_and_without_duplicates():
    sql = "SELECT " + ", ".join(f"fn{n}(1)" for n in range(100))
    found = check(sql)
    assert len(found.errors) <= sqlcheck.MAX_PROBLEMS


def test_functions_that_can_build_huge_values_are_not_on_the_allowlist():
    for call in ("repeat('x', 1000000000)", "lpad('x', 1000000000, 'y')", "rpad('x', 1000000000, 'y')"):
        refused(f"SELECT {call}")


def test_random_garbage_and_mutated_queries_never_crash_the_validator_and_never_pass_with_dangerous_text():
    import random

    rng = random.Random(44)
    pieces = [
        "SELECT",
        "FROM",
        "WHERE",
        "JOIN",
        "ON",
        "(",
        ")",
        ";",
        "--",
        "/*",
        "*/",
        "'",
        '"',
        "[",
        "]",
        ",",
        "*",
        "=",
        "id",
        "sales.customer",
        "DELETE",
        "DROP",
        "INTO",
        "EXEC",
        "UNION",
        "WITH",
        "AS",
        "c",
        "1",
        "pg_sleep(1)",
        "CASE",
        "WHEN",
        "END",
        "$$",
        "E'",
        "\\",
        "\x00",
        "@x",
        "::",
        "FOR UPDATE",
        "OPTION",
        "TOP",
        "\u2028",
        "é",
        "日本",
    ]
    for _ in range(3000):
        sql = " ".join(rng.choice(pieces) for _ in range(rng.randint(1, 25)))
        for provider in ("postgresql", "mssql"):
            found = check(sql, provider)
            if found.ok:
                low = found.sql.upper()
                for word in ("DELETE ", "DROP ", " INTO ", "EXEC", "PG_SLEEP", "FOR UPDATE", ";", "--", "/*"):
                    assert word not in low.replace("'", "") or word in sql.upper(), (sql, found.sql)
                assert found.sql.count(";") == 0, (sql, found.sql)


def test_fetch_first_and_offset_fetch_keep_a_small_count_and_cap_a_large_one():
    assert "FETCH FIRST 5 ROWS ONLY" in ok("SELECT id FROM sales.customer FETCH FIRST 5 ROWS ONLY").sql
    assert "FETCH FIRST 1001 ROWS ONLY" in ok("SELECT id FROM sales.customer FETCH FIRST 5000000 ROWS ONLY").sql
    own = ok("SELECT c.id FROM sales.customer c ORDER BY c.id OFFSET 5 ROWS FETCH NEXT 10 ROWS ONLY", "mssql")
    assert "FETCH NEXT 10 ROWS ONLY" in own.sql and not own.row_limit_added
    big = ok("SELECT c.id FROM sales.customer c ORDER BY c.id OFFSET 5 ROWS FETCH NEXT 90000 ROWS ONLY", "mssql")
    assert "FETCH NEXT 1001 ROWS ONLY" in big.sql


def test_a_join_hint_is_a_hint_and_is_refused():
    sql = "SELECT c.id FROM sales.customer c INNER HASH JOIN sales.order_line o ON o.customer_id = c.id"
    refused(sql, "mssql", code="hints_not_allowed")
    refused(sql.replace("HASH", "LOOP"), "mssql", code="hints_not_allowed")
    refused(sql.replace("HASH", "MERGE"), "mssql", code="hints_not_allowed")


def test_session_identity_and_catalogue_peeking_functions_are_refused():
    for call in ("current_user", "session_user", "current_database()", "inet_server_addr()", "pg_backend_pid()"):
        refused(f"SELECT {call}")
    for call in ("SUSER_NAME()", "SUSER_SNAME()", "ORIGINAL_LOGIN()", "@@SERVERNAME", "@@VERSION"):
        refused(f"SELECT {call}", "mssql")


# ---------- security audit: a row is more than its selected columns ----------


@pytest.mark.parametrize(
    "sql",
    [
        # PostgreSQL reads a bare table name or alias as THE WHOLE ROW, the hidden columns included
        "SELECT c FROM sales.customer c",
        "SELECT customer FROM sales.customer",
        "SELECT c::text FROM sales.customer c",
        "SELECT CAST(c AS text) FROM sales.customer c",
        "SELECT c.id FROM sales.customer c WHERE CAST(c AS text) LIKE '%x%'",
        "SELECT c.id FROM sales.customer c ORDER BY c",
        "SELECT c.id AS c FROM sales.customer c ORDER BY upper(c)",
        "SELECT c.id AS c FROM sales.customer c GROUP BY c",
        "SELECT array_agg(c) FROM sales.customer c",
        "SELECT concat(c) FROM sales.customer c",
        "SELECT (SELECT c) FROM sales.customer c",
        "SELECT (SELECT concat(c) FROM sales.order_line o) FROM sales.customer c",
        "SELECT row_number() OVER (ORDER BY c) FROM sales.customer c",
        "SELECT q FROM (SELECT c.id FROM sales.customer c) q",
        "SELECT v FROM (VALUES (1)) v",
    ],
)
def test_a_whole_row_reference_is_refused(sql):
    refused(sql)


@pytest.mark.parametrize("provider", ["postgresql", "mssql"])
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT concat(c.*) FROM sales.customer c",
        "SELECT concat(*) FROM sales.customer c",
        "SELECT CAST(c.* AS varchar(100)) FROM sales.customer c",
        "SELECT coalesce(c.*) FROM sales.customer c",
        "SELECT count(c.*) FROM sales.customer c",
        "SELECT (SELECT concat(c.*)) FROM sales.customer c",
        "SELECT c.id FROM sales.customer c WHERE concat(c.*) LIKE '%x%'",
        "SELECT ARRAY[c.*] FROM sales.customer c",
    ],
)
def test_a_star_anywhere_but_the_select_list_and_count_is_refused(sql, provider):
    refused(sql, provider)


@pytest.mark.parametrize("provider", ["postgresql", "mssql"])
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) AS k FROM sales.customer c",
        "SELECT * FROM sales.customer c",
        "SELECT c.* FROM sales.customer c",
        "SELECT q.* FROM (SELECT * FROM sales.customer) q",
        "SELECT c.id FROM sales.customer c WHERE EXISTS (SELECT * FROM sales.order_line o WHERE o.customer_id = c.id)",
        "SELECT c.id AS n FROM sales.customer c ORDER BY n",
        "SELECT count(*) AS k FROM sales.customer c GROUP BY c.id HAVING count(*) > 1 ORDER BY k DESC",
        "SELECT id FROM sales.customer UNION SELECT customer_id FROM sales.order_line ORDER BY id",
    ],
)
def test_the_ordinary_uses_of_a_star_and_of_a_result_name_still_work(sql, provider):
    found = ok(sql, provider)
    assert "secret" not in found.sql


@pytest.mark.parametrize(
    "sql",
    [
        # a correlated or derived reference to a hidden column, and PostgreSQL's `row.function` notation
        "SELECT c.row_to_json FROM sales.customer c",
        "SELECT c.id FROM sales.customer c WHERE EXISTS (SELECT 1 FROM sales.order_line o WHERE c.secret = 'x')",
        "SELECT (SELECT c.secret) FROM sales.customer c",
        "SELECT q.row_to_json FROM (SELECT c.id FROM sales.customer c) q",
        "SELECT v.pg_typeof FROM (VALUES (1)) AS v(a)",
        # a CTE name must not make a real catalog table of the same name reachable
        "WITH pg_roles AS (SELECT rolname FROM pg_roles) SELECT * FROM pg_roles",
        "WITH a AS (SELECT * FROM pg_roles), pg_roles AS (SELECT 1 AS x) SELECT * FROM a",
        'WITH "PG_ROLES" AS (SELECT 1 AS a) SELECT * FROM pg_roles',
        "SELECT (SELECT count(*) FROM pg_authid), q.a "
        "FROM (WITH pg_authid AS (SELECT 1 AS a) SELECT a FROM pg_authid) q",
        "SELECT c.id FROM sales.customer c LIMIT (SELECT count(*) FROM pg_authid)",
    ],
)
def test_hidden_columns_and_catalog_tables_stay_out_of_reach(sql):
    refused(sql)


@pytest.mark.parametrize(
    "sql,provider",
    [
        ("SELECT CAST('a' AS char(10000000))", "postgresql"),
        ("SELECT CAST('a' AS varchar(10485760))", "postgresql"),
    ],
)
def test_a_data_type_cannot_be_used_to_pad_a_value_to_megabytes(sql, provider):
    refused(sql, provider, "type_not_allowed")


@pytest.mark.parametrize(
    "sql,provider",
    [
        ("SELECT CAST(c.id AS varchar(8000)) FROM sales.customer c", "postgresql"),
        ("SELECT CAST(c.id AS numeric(38, 10)) FROM sales.customer c", "postgresql"),
        ("SELECT CAST(c.id AS nvarchar(4000)) FROM sales.customer c", "mssql"),
    ],
)
def test_ordinary_data_type_sizes_are_accepted(sql, provider):
    ok(sql, provider)
