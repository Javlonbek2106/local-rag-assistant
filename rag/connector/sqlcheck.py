"""SQL validation (roadmap M4.4): is this text ONE read-only query over objects the owner has made available, and what is
the exact text that may be run? Pure: no database, no network. SQLGlot (an AST library, lazily imported) does the
parsing; nothing here is decided by looking for keywords.

How it decides (docs/sql-validation.md):

1. **Parse** the whole text with the dialect of the connection (`postgres` or `tsql`). A syntax error, an empty text, a
   text with more than one statement, or anything the parser could only read as an opaque "command" is refused.
2. **Allowlist, not denylist.** Every node of the syntax tree must be a kind this module knows to be a read-only part of
   a query (ALLOWED_NODES), every function must be on the function allowlist (ALLOWED_FUNCTIONS, ALLOWED_ANONYMOUS),
   every data type a builtin one. Whatever is not listed is refused, with the reason it is most likely to be dangerous:
   DML and DDL (also nested, as in `WITH d AS (DELETE ... RETURNING *)`), EXEC and procedures, SELECT INTO, locking
   clauses, table hints and query options, variables and session settings, table functions, OPENROWSET/OPENQUERY and
   friends, four-part names, recursive CTEs, a function nobody listed (`pg_read_file`, `xp_cmdshell`, `dblink`,
   `pg_sleep`...).
3. **Schema.** Every table must be an object of the connection's CURRENT snapshot that the owner has SELECTED, every
   column one the selection shows (an excluded column does not exist for a query). Aliases, CTEs, subqueries and joins
   are resolved by the optimizer's scope analysis with the real names of the snapshot as the schema.
4. **Canonical text.** What may be run is not what was typed: it is the tree written out again, with every table written
   as its real quoted `schema.name`, every column of a real table by its real quoted name, `*` expanded to the columns
   the selection shows (so `SELECT *` cannot return an excluded column), no comments, and a row limit added. The server
   parses this text, not the original: comments, odd quoting and parser differences cannot carry anything past the
   check.

What it does NOT promise: that the query is cheap (limits and the database's own timeout do that), that functions on the
allowlist are free of surprises, or that the database account is read-only (the driver refuses accounts that can write).
"""

import logging
import re

from rag.connector.errors import ConnectorError

DIALECTS = {"postgresql": "postgres", "mssql": "tsql"}
MAX_PROBLEMS = 20

# Node kinds that are plain, read-only parts of a query. Anything else is refused.
ALLOWED_NODES = frozenset(
    """Add Alias And Any Between Bitwise BitwiseAnd BitwiseLeftShift BitwiseNot BitwiseOr BitwiseRightShift BitwiseXor
    Boolean Bracket CTE Case Cast Column Concat Connector DPipe DataType DataTypeParam Distinct Div EQ Except Exists Fetch
    Filter From GT GTE Group GroupingSets Having ILike Identifier In Interval Intersect Is IntDiv JSONPath JSONPathKey
    JSONPathRoot JSONPathSubscript Join LT LTE Like Limit LimitOptions Literal Mod Mul NEQ National Neg Not Null
    NullSafeEQ NullSafeNEQ Offset Or Order Ordered Paren Rollup Cube Select SetOperation SimilarTo Star Sub Subquery Table
    TableAlias Tuple Union Values Var Where Window WindowSpec With WithinGroup Pow Xor""".split()
)

# Functions SQLGlot models (by class). Aggregates, window functions, conversions, text, number, date and null handling.
ALLOWED_FUNCTIONS = frozenset(
    """Abs Array ArrayAgg ArrayContainsAll Avg Ceil Coalesce Concat ConcatWs Convert Count CountIf CumeDist CurrentDate
    CurrentDatetime CurrentTime CurrentTimestamp Date DateAdd DateDiff DateFromParts DateStrToDate DateSub DateToDateStr
    DateTrunc Datetime DatetimeAdd DatetimeDiff DatetimeSub DatetimeTrunc Day DayOfMonth DayOfWeek DayOfYear DenseRank
    Exp Extract First FirstValue Floor GroupConcat Greatest If Initcap JSONExtract JSONExtractScalar Lag LastDay LastValue
    Lead Least Left Length Ln Log Lower Ltrim Max Min Month Ntile NthValue Nullif PercentRank PercentileCont
    PercentileDisc Pow Power Quarter Rank RegexpLike Replace Reverse Right Round Rtrim RowNumber Sign Sqrt
    Stddev StddevPop StddevSamp StrPosition StringAgg Substring Sum TimeStrToTime TimeToStr Timestamp TimestampTrunc
    ToChar Trim TryCast TsOrDsAdd TsOrDsDiff TsOrDsToDate TsOrDsToTimestamp Unhex Upper Variance VariancePop Week
    WeekOfYear Year LogicalAnd LogicalOr Mode Median Cbrt Trunc""".split()
)

# Functions SQLGlot does not model (it keeps their name): the same kind of thing, in the spelling of each dialect.
ALLOWED_ANONYMOUS = frozenset(
    """abs avg btrim ceil ceiling char_length character_length charindex coalesce concat concat_ws count count_big
    current_date current_timestamp date_part date_trunc dateadd datediff datename datepart day eomonth floor
    getdate getutcdate greatest iif isnull isdate least left len length lower ltrim max min month nullif now
    patindex power quarter reverse right round rtrim sign sqrt stdev stdevp str string_agg substr substring sum
    sysdatetime sysdatetimeoffset sysutcdatetime to_char to_date to_number to_timestamp translate trim try_convert
    try_cast try_parse upper var varp year isnumeric""".split()
)

# The unit of a date function (`DATEADD(day, ...)`, `INTERVAL '1' DAY`) is a bare word; nowhere else is one accepted.
UNIT_PARENTS = frozenset(
    """Interval DateAdd DateSub DateDiff DatetimeAdd DatetimeSub DatetimeDiff TsOrDsAdd TsOrDsDiff Extract DateTrunc
    TimestampTrunc DatetimeTrunc LastDay WindowSpec Convert Anonymous TimeStrToTime""".split()
)
UNIT = re.compile(r"^[A-Za-z_]{1,32}$")
# The largest size a data type may be written with: PostgreSQL pads `char(n)` to n characters, so an unbounded size
# would build values of megabytes, like the padding functions that are not on the allowlist.
MAX_TYPE_SIZE = 10000

# Why a node is refused, by its kind (the first match wins). Unlisted kinds: unsupported_construct.
REFUSED = (
    ("dml_not_allowed", "Insert Update Delete Merge Returning Whens"),
    (
        "ddl_not_allowed",
        "Create Alter AlterColumn AlterRename AlterSet Drop TruncateTable Comment Analyze Cache Uncache Refresh",
    ),
    ("permission_statement_not_allowed", "Grant Revoke"),
    ("exec_not_allowed", "Execute Command Kill"),
    ("copy_not_allowed", "Copy LoadData Attach Detach Pragma"),
    ("session_statement_not_allowed", "Set SetItem Use Describe Show"),
    ("transaction_statement_not_allowed", "Transaction Commit Rollback"),
    ("variables_not_allowed", "Declare DeclareItem Parameter Placeholder SessionParameter"),
    ("select_into_not_allowed", "Into"),
    ("locking_not_allowed", "Lock"),
    ("hints_not_allowed", "Hint QueryOption WithTableHint"),
    ("table_function_not_allowed", "Lateral Unnest Explode TableFromRows ExplodingGenerateSeries"),
    ("unsupported_construct", "TableSample Pivot Dot"),
)
MESSAGES = {
    "dml_not_allowed": "data-changing statements (INSERT, UPDATE, DELETE, MERGE) are not allowed, also inside a query",
    "ddl_not_allowed": "schema-changing statements (CREATE, ALTER, DROP, TRUNCATE) are not allowed",
    "permission_statement_not_allowed": "GRANT and REVOKE are not allowed",
    "exec_not_allowed": "EXEC, stored procedures and opaque commands are not allowed",
    "copy_not_allowed": "COPY, import/export, ATTACH and PRAGMA are not allowed",
    "session_statement_not_allowed": "session and administrative statements (SET, USE, SHOW) are not allowed",
    "transaction_statement_not_allowed": "transaction control is not allowed",
    "variables_not_allowed": "variables, parameters and session variables are not allowed",
    "select_into_not_allowed": "SELECT ... INTO creates a table and is not allowed",
    "locking_not_allowed": "locking clauses (FOR UPDATE, FOR SHARE) are not allowed",
    "hints_not_allowed": "hints, table hints and query options are not allowed",
    "table_function_not_allowed": "table-valued functions and LATERAL are not allowed",
    "unsupported_construct": "this construct is not supported by the validator",
}
DANGEROUS_PREFIXES = ("pg_", "lo_", "dblink", "xp_", "sp_", "open", "fn_", "dbms_", "utl_", "sys")


REFUSED_BY_CLASS = {}
for _code, _names in REFUSED:
    for _name in _names.split():
        REFUSED_BY_CLASS.setdefault(_name, _code)

_sqlglot_module = None


def _sqlglot():
    """SQLGlot, imported when first used (it is an optional library of the connector); its own logging is silenced,
    because it logs the text of the statement it could not read, and a query must not reach the application's log."""
    global _sqlglot_module
    if _sqlglot_module is None:
        try:
            import sqlglot
            from sqlglot import exp
            from sqlglot.optimizer.qualify import qualify
            from sqlglot.optimizer.scope import traverse_scope
            from sqlglot.schema import MappingSchema
        except ImportError:
            raise ConnectorError("sql_validator_unavailable", 503) from None
        logger = logging.getLogger("sqlglot")
        logger.setLevel(logging.CRITICAL + 1)
        logger.propagate = False

        class Glot:
            pass

        glot = Glot()
        glot.sqlglot, glot.exp, glot.qualify, glot.traverse_scope, glot.MappingSchema = (
            sqlglot,
            exp,
            qualify,
            traverse_scope,
            MappingSchema,
        )
        _sqlglot_module = glot
    return _sqlglot_module


def problem(code, message, **extra):
    return {"code": code, "message": message, **extra}


class Analysis:
    """The outcome of analyze(): the findings, what the query refers to, and the one text that may be run."""

    def __init__(self, provider):
        self.provider = provider
        self.errors, self.warnings = [], []
        self.tables, self.functions = [], []
        self.sql = None  # the canonical text, only when there is no error
        self.row_limit_added = False

    @property
    def ok(self):
        return not self.errors and self.sql is not None

    def error(self, code, message, **extra):
        if len(self.errors) < MAX_PROBLEMS and not any(
            e["code"] == code and e["message"] == message for e in self.errors
        ):
            self.errors.append(problem(code, message, **extra))

    def warn(self, code, message, **extra):
        if len(self.warnings) < MAX_PROBLEMS:
            self.warnings.append(problem(code, message, **extra))


class Limits:
    """What bounds the analysis itself (the size of the text and of the tree) and the rows a query may return."""

    def __init__(self, max_chars, max_nodes, max_depth, row_cap):
        self.max_chars, self.max_nodes, self.max_depth, self.row_cap = max_chars, max_nodes, max_depth, row_cap


def _syntax_message(error):
    errors = getattr(error, "errors", None) or []
    if errors:
        first = errors[0]
        where = f" (line {first.get('line')}, column {first.get('col')})" if first.get("line") else ""
        return f"the SQL could not be read{where}"
    return "the SQL could not be read"


def _statement(sql, dialect, glot, analysis, limits):
    """The single statement of the text as a tree, or None (with the reason in `analysis`)."""
    if not isinstance(sql, str) or not sql.strip():
        analysis.error("empty_sql", "there is no SQL")
        return None
    if len(sql) > limits.max_chars:
        analysis.error("sql_too_large", f"the SQL is longer than {limits.max_chars} characters")
        return None
    if "\x00" in sql:
        analysis.error("invalid_characters", "the SQL contains a NUL character")
        return None
    try:
        trees = glot.sqlglot.parse(sql, read=dialect)
    except RecursionError:
        analysis.error("too_complex", "the SQL is nested too deeply")
        return None
    except (glot.sqlglot.errors.ParseError, glot.sqlglot.errors.TokenError) as error:
        analysis.error("syntax_error", _syntax_message(error))
        return None
    except Exception:  # noqa: BLE001 - whatever the parser raises for a text it cannot take is a refusal, not a crash
        analysis.error("syntax_error", "the SQL could not be read")
        return None
    trees = [t for t in trees if t is not None]
    if not trees:
        analysis.error("empty_sql", "there is no SQL (only comments or separators)")
        return None
    if len(trees) > 1:
        analysis.error("multiple_statements", "only one statement is allowed")
        return None
    return trees[0]


def _function_name(node, exp):
    if isinstance(node, exp.Anonymous):
        return str(node.name or "").lower()
    return type(node).__name__


def _structure(tree, glot, analysis, limits):
    """The allowlist walk. Returns False when nothing more can be said (too big); problems are in `analysis`."""
    exp = glot.exp
    functions = set()
    count = 0
    for node in tree.walk():
        count += 1
        if count > limits.max_nodes:
            analysis.error("too_complex", f"the SQL has more than {limits.max_nodes} parts")
            return False
        if node.depth > limits.max_depth:
            analysis.error("too_complex", f"the SQL is nested deeper than {limits.max_depth} levels")
            return False
        name = type(node).__name__
        if isinstance(node, exp.Anonymous):
            fname = _function_name(node, exp)
            functions.add(fname)
            if fname not in ALLOWED_ANONYMOUS:
                code = "dangerous_function" if fname.startswith(DANGEROUS_PREFIXES) else "function_not_allowed"
                analysis.error(code, f"the function {fname or '?'}() is not allowed", function=fname)
            continue
        if name in REFUSED_BY_CLASS and name not in ALLOWED_NODES:
            code = REFUSED_BY_CLASS[name]
            analysis.error(code, MESSAGES[code])
            continue
        if isinstance(node, exp.Func) and name not in ALLOWED_NODES:
            functions.add(name.lower())
            if name not in ALLOWED_FUNCTIONS:
                analysis.error("function_not_allowed", f"the function {name} is not allowed", function=name.lower())
            continue
        if name not in ALLOWED_NODES and name not in ALLOWED_FUNCTIONS:
            analysis.error("unsupported_construct", f"{name} is not supported by the validator", construct=name)
            continue
        if isinstance(node, exp.Func):
            functions.add(name.lower())
        if isinstance(node, exp.With) and node.args.get("recursive"):
            analysis.error("recursive_cte_not_allowed", "recursive CTEs are not allowed")
        elif isinstance(node, exp.DataType):
            if node.this in (exp.DataType.Type.USERDEFINED, exp.DataType.Type.UNKNOWN):
                analysis.error("type_not_allowed", "only built-in data types are allowed")
        elif isinstance(node, exp.DataTypeParam):
            size = str(node.this.name) if isinstance(node.this, exp.Literal) else ""
            if size.isdigit() and int(size) > MAX_TYPE_SIZE:
                analysis.error("type_not_allowed", f"a data type size above {MAX_TYPE_SIZE} is not allowed")
        elif isinstance(node, exp.Var):
            parent = type(node.parent).__name__ if node.parent is not None else ""
            if parent not in UNIT_PARENTS or not UNIT.match(str(node.name or "")):
                analysis.error("unsupported_construct", "a bare word is only allowed as the unit of a date function")
        elif isinstance(node, exp.Table):
            if not isinstance(node.this, exp.Identifier):
                analysis.error("table_function_not_allowed", "a table must be named, not produced by a function")
            if node.args.get("catalog"):
                analysis.error("cross_database_not_allowed", "names with a database or server part are not allowed")
        elif isinstance(node, exp.Subquery) and node.args.get("pivots"):
            analysis.error("unsupported_construct", "PIVOT is not supported")
    analysis.functions = sorted(functions)
    return True


def _stars(tree, glot, analysis, expanded=False):
    """`*` stands for columns only as an item of a select list (where it is expanded to the columns the selection shows)
    and in COUNT(*). Anywhere else (`concat(t.*)`, `CAST(t.* AS text)`) the database reads it as the whole row, hidden
    columns included. With `expanded` (after the optimizer) not even a select list may still hold one."""
    exp = glot.exp
    for star in tree.find_all(exp.Star):
        if isinstance(star.parent, exp.Count) and star.parent.this is star:
            continue
        item = star.parent if isinstance(star.parent, exp.Column) else star
        listed = isinstance(item.parent, exp.Select) and item.arg_key == "expressions"
        if expanded or not listed or any(star.args.values()):
            analysis.error("star_not_allowed", "* is only allowed in a select list and in COUNT(*)")


def _result_name(column, glot):
    """Is this unqualified name a column of the RESULT, where a database reads it as one: a bare item of the query's own
    ORDER BY (or of DISTINCT ON)?"""
    exp = glot.exp
    parent = column.parent
    if isinstance(parent, exp.Ordered) and column.arg_key == "this":
        order = parent.parent
        query = order.parent if isinstance(order, exp.Order) and order.arg_key == "order" else None
    elif isinstance(parent, exp.Tuple) and isinstance(parent.parent, exp.Distinct) and parent.arg_key == "on":
        query = parent.parent.parent
    else:
        return False
    return isinstance(query, (exp.Select, exp.SetOperation)) and str(column.name) in query.named_selects


def _whole_rows(tree, glot, analysis):
    """PostgreSQL reads the name of a table or of an alias as THE WHOLE ROW, hidden columns included. The optimizer
    turns such a name into a TableColumn node, or leaves it a column without a table (a column of a table always gets
    one). Only a name of a result column, where it is read as that, may stay without a table."""
    for column in tree.find_all(glot.exp.Column, glot.exp.TableColumn):
        if isinstance(column, glot.exp.TableColumn) or (not column.table and not _result_name(column, glot)):
            analysis.error(
                "unknown_column",
                f"the column {column.name} does not exist or is not selected (a whole row cannot be read)",
                column=str(column.name),
            )


def _root(tree, glot, analysis):
    exp = glot.exp
    if isinstance(tree, (exp.Select, exp.SetOperation, exp.Subquery)):
        return True
    name = type(tree).__name__
    code = REFUSED_BY_CLASS.get(name, "statement_not_allowed")
    analysis.error(code, MESSAGES.get(code, f"a {name} statement is not allowed: only SELECT queries are"))
    return False


# ---------- names ----------


def _match(real, written, quoted, provider):
    """Does the real name `real` answer to the name as written? PostgreSQL folds an unquoted name to lower case and keeps
    a quoted one; SQL Server compares names without regard to case."""
    if provider == "postgresql":
        return real == (written if quoted else written.lower())
    return real.lower() == written.lower()


def _identifier(node):
    return (str(node.name), bool(node.args.get("quoted"))) if node is not None else (None, False)


def _resolve_tables(tree, glot, provider, catalog, analysis):
    """Point every table of the query at a real, selected object, writing the real name into the tree. Returns the list of
    real objects (schema, name) the query uses."""
    exp = glot.exp
    objects = catalog.objects()  # {(schema, name): (type, selected)}
    cte_names = {str(cte.alias).lower() for cte in tree.find_all(exp.CTE) if cte.alias}
    used = {}
    for table in list(tree.find_all(exp.Table)):
        if not isinstance(table.this, exp.Identifier) or table.args.get("catalog"):
            continue  # already refused by the walk
        name, name_quoted = _identifier(table.this)
        schema, schema_quoted = _identifier(table.args.get("db"))
        if schema is None and name.lower() in cte_names:
            continue  # a reference to a CTE: the scope analysis decides whether it is one
        candidates = [
            key
            for key in objects
            if _match(key[1], name, name_quoted, provider)
            and (schema is None or _match(key[0], schema, schema_quoted, provider))
        ]
        shown = f"{schema + '.' if schema else ''}{name}"
        if not candidates:
            analysis.error("unknown_table", f"{shown} is not an object of this database", table=shown)
            continue
        if len(candidates) > 1:
            analysis.error("ambiguous_table", f"{shown} matches several objects: write it as schema.name", table=shown)
            continue
        key = candidates[0]
        if not objects[key][1]:
            analysis.error("table_not_selected", f"{key[0]}.{key[1]} is not selected for use in queries", table=shown)
            continue
        if table.args.get("alias") is None:  # the name the query uses for the table stays what the person wrote
            table.set("alias", exp.TableAlias(this=exp.Identifier(this=name, quoted=name_quoted)))
        table.set("db", exp.Identifier(this=key[0], quoted=True))
        table.set("this", exp.Identifier(this=key[1], quoted=True))
        table.meta["rag_real"] = key
        used[key] = True
    return list(used)


# ---------- the tree after the optimizer ----------

UNKNOWN_COLUMN = (re.compile(r"Column '([^']{1,128})'"), re.compile(r"Unknown column: (\S{1,128})"))


def _qualify(tree, glot, provider, dialect, catalog, keys, analysis):
    """Resolve aliases, CTEs, subqueries and `*` with the REAL, SELECTED columns of the tables. Returns (tree, names) where
    names[(schema, table)] maps what a query may say to the real column name; (None, names) with the reason in
    `analysis`."""
    names, mapping = {}, {}
    for key in keys:
        shown = {}
        for column, _kind in catalog.columns(key):
            said = column if provider == "postgresql" else column.lower()
            if said in shown:
                analysis.error(
                    "ambiguous_column",
                    f"{key[0]}.{key[1]} has columns that differ only by case: {column}",
                    table=key[1],
                )
            shown[said] = column
        names[key] = shown
        mapping.setdefault(key[0], {})[key[1]] = {said: "text" for said in shown}
    if analysis.errors:
        return None, names
    schema = glot.MappingSchema(mapping, dialect=dialect, normalize=False)
    try:
        tree = glot.qualify(
            tree, dialect=dialect, schema=schema, validate_qualify_columns=True, quote_identifiers=False, identify=False
        )
    except glot.sqlglot.errors.OptimizeError as error:
        text = str(error)
        found = next((m.group(1) for m in (r.search(text) for r in UNKNOWN_COLUMN) if m), None)
        owners = [k for k in keys if found and found.lower() in {c.lower() for c in names[k]}]
        if "mbiguous" in text or len(owners) > 1:
            analysis.error(
                "ambiguous_column", "a column name matches more than one table: qualify it with the table alias"
            )
        elif found:
            analysis.error("unknown_column", f"the column {found} does not exist or is not selected", column=found)
        else:
            analysis.error("unresolved_reference", "a table, alias or column in the query could not be resolved")
        return None, names
    except RecursionError:
        analysis.error("too_complex", "the SQL is nested too deeply")
        return None, names
    except Exception:  # noqa: BLE001 - the optimizer could not make sense of it: refuse rather than guess
        analysis.error("unresolved_reference", "a table, alias or column in the query could not be resolved")
        return None, names
    return tree, names


def _scopes(tree, glot, analysis):
    """[(scope, {alias: real table})] after qualification; every table source must be a real, selected table."""
    exp = glot.exp
    found = []
    try:
        scopes = glot.traverse_scope(tree)
    except Exception:  # noqa: BLE001
        analysis.error("unresolved_reference", "a table, alias or column in the query could not be resolved")
        return found
    for scope in scopes:
        real = {}
        for alias, source in scope.sources.items():
            if isinstance(source, exp.Table):
                key = source.meta.get("rag_real")
                if key is None:
                    analysis.error("unknown_table", f"{alias} is not a table of this database", table=str(alias))
                else:
                    real[alias] = key
        found.append((scope, real))
    return found


def _joins(tree, scopes, glot, analysis, catalog):
    """Joins need a condition; a join not on a known relationship is a warning, not an error."""
    exp = glot.exp
    real_of = {id(scope.expression): real for scope, real in scopes}
    pairs_of = {}
    for join in tree.find_all(exp.Join):
        owner = join.find_ancestor(exp.Select)
        real = real_of.get(id(owner), {})
        method = str(join.args.get("method") or "").upper()
        if method == "NATURAL":
            analysis.error("natural_join_not_allowed", "NATURAL JOIN is not allowed: write the join condition")
            continue
        if method or join.args.get("hint"):  # HASH, LOOP, MERGE, REMOTE: a join hint
            analysis.error("hints_not_allowed", MESSAGES["hints_not_allowed"])
            continue
        on = join.args.get("on")
        if str(join.args.get("kind") or "").upper() == "CROSS" or (on is None and not join.args.get("using")):
            analysis.error(
                "join_without_condition", "every join needs a condition (ON or USING): a cross join is not allowed"
            )
            continue
        if on is None:
            continue
        equal = [
            (c.left, c.right)
            for c in on.find_all(exp.EQ)
            if isinstance(c.left, exp.Column) and isinstance(c.right, exp.Column)
        ]
        if not equal:
            analysis.warn("join_condition_not_a_key_equality", "the join condition does not compare two columns")
            continue
        known = False
        for left, right in equal:
            a, b = real.get(left.table), real.get(right.table)
            if a is None or b is None:
                continue
            for first, second, lcol, rcol in ((a, b, left, right), (b, a, right, left)):
                if first not in pairs_of:
                    pairs_of[first] = catalog.foreign_keys(first)
                for columns, ref_schema, ref_name, ref_columns in pairs_of[first]:
                    if (ref_schema.lower(), ref_name.lower()) == (second[0].lower(), second[1].lower()):
                        wanted = {c.lower(): r.lower() for c, r in zip(columns, ref_columns, strict=False)}
                        if wanted.get(str(lcol.name).lower()) == str(rcol.name).lower():
                            known = True
        if not known:
            analysis.warn("join_not_on_known_relationship", "this join is not on a relationship known to the schema")


def _restore(tree, glot, provider, scopes, names):
    """Write the real names back (a real column keeps its real name, also as the name it has in the result) and quote
    every name, so that the text means the same on a server with another case rule."""
    exp = glot.exp
    for node in tree.walk():
        node.comments = None  # comments are not part of the query: none of them is written out again
    for scope, real in scopes:
        for column in scope.columns:
            key = real.get(column.table)
            if key is None or isinstance(column.this, exp.Star):
                continue
            written = str(column.name)
            actual = names[key].get(written if provider == "postgresql" else written.lower())
            if actual:
                column.set("this", exp.Identifier(this=actual, quoted=True))
    for scope, real in scopes:
        if scope.expression is not tree:
            continue
        for item in getattr(tree, "expressions", None) or []:
            if isinstance(item, exp.Alias) and isinstance(item.this, exp.Column) and item.this.table in real:
                if str(item.alias).lower() == str(item.this.name).lower():
                    item.set("alias", exp.Identifier(this=str(item.this.name), quoted=True))
    for ident in tree.find_all(exp.Identifier):
        ident.set("quoted", True)


def _row_limit(tree, glot, provider, cap, analysis):
    """A plain SELECT gets `LIMIT cap+1` (`TOP` on SQL Server) unless it has a small one of its own: the server stops
    working at that many rows (one more than the cap, to see that there were more). Unions are bounded by the driver."""
    exp = glot.exp
    if not isinstance(tree, exp.Select):
        return

    def small(count):
        return (
            isinstance(count, exp.Literal)
            and not count.is_string
            and str(count.this).isdigit()
            and int(count.this) <= cap + 1
        )

    limit = tree.args.get("limit")
    if isinstance(limit, exp.Fetch):  # FETCH FIRST/NEXT n ROWS ONLY: keep a small n, cap a large or unknown one
        options = limit.args.get("limit_options")
        if not small(limit.args.get("count")) or (options is not None and options.args.get("percent")):
            limit.set("count", exp.Literal.number(cap + 1))
            limit.set("limit_options", exp.LimitOptions(percent=False, rows=True, with_ties=False))
            analysis.row_limit_added = True
        return
    if provider == "mssql" and tree.args.get("offset"):
        return
    options = limit.args.get("limit_options") if limit is not None else None
    if options is not None and options.args.get("percent"):
        return  # TOP n PERCENT: the driver cuts what comes back
    if small(limit.expression if limit is not None else None):
        return
    tree.limit(cap + 1, copy=False)
    analysis.row_limit_added = True


def analyze(sql, provider, catalog, limits):
    """Everything the validator can say about `sql` for a connection of this `provider`, against `catalog` (see
    snapshots.SelectedCatalog: objects(), load(keys), columns(key), foreign_keys(key)). `analysis.ok` and
    `analysis.sql` (the one text that may be run) are only set when there is no error."""
    analysis = Analysis(provider)
    dialect = DIALECTS.get(provider)
    if dialect is None:
        analysis.error("dialect_not_supported", "SQL cannot be validated for this kind of database")
        return analysis
    glot = _sqlglot()
    tree = _statement(sql, dialect, glot, analysis, limits)
    if tree is None or not _root(tree, glot, analysis):
        return analysis
    if not _structure(tree, glot, analysis, limits) or analysis.errors:
        return analysis
    _stars(tree, glot, analysis)
    if analysis.errors:
        return analysis
    if provider == "mssql":  # names are compared without regard to case: say them all in one case before resolving
        for ident in tree.find_all(glot.exp.Identifier):
            ident.set("this", str(ident.name).lower())
            ident.set("quoted", False)
    keys = _resolve_tables(tree, glot, provider, catalog, analysis)
    if analysis.errors:
        return analysis
    catalog.load(keys)
    tree, names = _qualify(tree, glot, provider, dialect, catalog, keys, analysis)
    if tree is None:
        return analysis
    scopes = _scopes(tree, glot, analysis)
    _stars(tree, glot, analysis, expanded=True)
    _whole_rows(tree, glot, analysis)
    if analysis.errors:
        return analysis
    written = analysis.functions  # (what the person wrote, not what the optimizer wrote)
    _structure(tree, glot, analysis, limits)  # the optimizer rewrites the tree: what it made must be allowed too
    analysis.functions = written
    if analysis.errors:
        return analysis
    _joins(tree, scopes, glot, analysis, catalog)
    if analysis.errors:
        return analysis
    used = {}
    for scope, real in scopes:
        for key in real.values():
            used.setdefault(key, set())
        for column in scope.columns:
            key = real.get(column.table)
            if key is not None and not isinstance(column.this, glot.exp.Star):
                said = str(column.name) if provider == "postgresql" else str(column.name).lower()
                used[key].add(names[key].get(said, str(column.name)))
    analysis.tables = [{"schema": k[0], "name": k[1], "columns": sorted(v)} for k, v in sorted(used.items())]
    try:
        _restore(tree, glot, provider, scopes, names)
        _row_limit(tree, glot, provider, limits.row_cap, analysis)
        analysis.sql = tree.sql(dialect=dialect)
    except RecursionError:
        analysis.error("too_complex", "the SQL is nested too deeply")
    except Exception:  # noqa: BLE001
        analysis.error("unsupported_construct", "the SQL could not be written out again")
    if analysis.errors:
        analysis.sql = None
    return analysis
