"""M4.1b: the boundary rules of roadmap 6a.7, as tests (the way scripts/audit checks of E9 were)."""

import ast
from pathlib import Path

from rag import config

PACKAGE = Path(config.ROOT) / "rag" / "connector"
RAG = Path(config.ROOT) / "rag"
DOCUMENT_SIDE = ("rag.ingest", "rag.store", "rag.versioning", "rag.worker", "rag.storage", "rag.answer")
APPLICATION_SIDE = DOCUMENT_SIDE + (
    "rag.auth",
    "rag.api",
    "rag.connector_wiring",
    "rag.connectorcli",
    "rag.health",
    "rag.llm",
    "rag.relay",
    "rag.dbroles",
    "rag.dbadmin",
    "rag.backup",
    "rag.reconcile",
)
# Third-party libraries of the connector: imported only inside functions, never at the top of a module.
LAZY_LIBRARIES = ("cryptography", "pytds", "OpenSSL")


def imports(path, top_level_only=False):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = tree.body if top_level_only else ast.walk(tree)
    found = []
    for node in nodes:
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append(node.module)
            found += [f"{node.module}.{alias.name}" for alias in node.names]
    return found


def starts(name, prefixes):
    return any(name == p or name.startswith(p + ".") for p in prefixes)


def test_the_connector_imports_nothing_of_the_document_side_or_the_authentication_layer():
    for path in PACKAGE.rglob("*.py"):
        bad = [name for name in imports(path) if starts(name, APPLICATION_SIDE)]
        assert not bad, f"{path.relative_to(RAG)} imports {bad}"


DATABASE_MODULES = {
    "repository.py",
    "snapshots.py",
}  # the repository and its part for snapshots, changes and selections


def test_only_the_repository_modules_touch_the_internal_database_and_the_drivers_never_do():
    for path in PACKAGE.rglob("*.py"):
        uses_db = "rag.db" in imports(path)
        assert uses_db == (path.name in DATABASE_MODULES), f"{path.relative_to(RAG)}: rag.db imported = {uses_db}"


def test_the_document_side_does_not_import_the_connector():
    allowed = {"api.py", "connector_wiring.py", "connectorcli.py", "dbroles.py", "dbadmin.py", "sqlmodel.py"}
    for path in RAG.glob("*.py"):
        if path.name in allowed:
            continue
        bad = [name for name in imports(path) if starts(name, ("rag.connector", "rag.connector_wiring"))]
        assert not bad, f"{path.name} imports {bad}"
    assert not [n for n in imports(RAG / "dbroles.py") if starts(n, ("rag.connector",))]


def test_third_party_libraries_of_the_connector_are_imported_lazily():
    for path in PACKAGE.rglob("*.py"):
        top = [name for name in imports(path, top_level_only=True) if starts(name, LAZY_LIBRARIES)]
        assert not top, f"{path.relative_to(RAG)} imports {top} at the top of the module"
    for name in ("api.py", "main.py"):
        if (RAG / name).exists():
            assert not [n for n in imports(RAG / name, top_level_only=True) if starts(n, LAZY_LIBRARIES)]


def functions_with_sql(path):
    """(function name, its source) for every function of a module that sends SQL (`.execute` / `.executemany`)."""
    source = path.read_text(encoding="utf-8")
    found = []
    for function in ast.walk(ast.parse(source)):
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sends = any(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr in ("execute", "executemany")
                for n in ast.walk(function)
            )
            if sends:
                found.append((function.name, ast.get_source_segment(source, function)))
    return found


def test_every_function_of_the_repository_that_reaches_a_connection_names_the_owner():
    exempt = {
        "principals",  # the list of principals, for the operator tools
        # the writer's inserts, after the owner of the connection was proved in the same transaction
        "flush",
        # snapshots.py: these run on the connection of repository.schema_replacement, inside the transaction that locked
        # the owner's connection row first, and take only ids that transaction made
        "previous_snapshot",
        "new_snapshot",
        "fingerprint",
        "counts",
        "compute_changes",
        "prune",
    }
    functions = functions_with_sql(PACKAGE / "repository.py") + functions_with_sql(PACKAGE / "snapshots.py")
    assert len(functions) >= 30
    for name, text in functions:
        if name in exempt:
            continue
        touches = any(word in text for word in ("connections", "connection_"))
        # (_owned: checked first)
        assert not touches or "owner_principal_id" in text or "_owned(conn, owner" in text, name


def test_the_credential_columns_are_named_in_only_a_few_functions():
    named = {name for name, text in functions_with_sql(PACKAGE / "repository.py") if "credential_" in text}
    assert named == {"insert", "update", "get_sealed", "all_sealed", "replace_sealed"}
    source = (PACKAGE / "repository.py").read_text(encoding="utf-8")
    view = source.split("VIEW = (")[1].split("EDITABLE")[0]  # the list of columns a connection is shown with
    assert "credential_ciphertext" not in view and "credential_nonce" not in view


def test_no_statement_is_built_from_a_request_value():
    """The repository puts into SQL text only fixed pieces of its own (column lists, WHERE fragments); every value that
    came from a request is a parameter."""
    source = (PACKAGE / "repository.py").read_text(encoding="utf-8")
    assert "% (" not in source and ".format(" not in source and "+ owner" not in source
    more = (PACKAGE / "snapshots.py").read_text(encoding="utf-8")
    assert "% (" not in more and ".format(" not in more and "+ owner" not in more


def test_sql_generation_cannot_reach_an_external_database_or_the_model_client():
    """M4.3 generates text. The generator imports no driver, no database library and no model client, and no driver
    has a function that could run a query: the drivers only test a connection and read the catalogue."""
    forbidden = ("rag.connector.drivers", "psycopg", "pytds", "rag.llm", "socket", "http", "urllib", "subprocess")
    for name in ("sqlgen.py",):
        bad = [n for n in imports(PACKAGE / name) if starts(n, forbidden)]
        assert not bad, f"{name} imports {bad}"
    for path in (PACKAGE / "drivers").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        public = {n.name for n in tree.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")}
        assert not {name for name in public if any(w in name for w in ("execute", "query", "run", "fetch"))}, path.name


def test_the_service_hands_the_generator_a_model_function_and_never_imports_one():
    source = (PACKAGE / "service.py").read_text(encoding="utf-8")
    assert "sql_completer" in source and not [
        n for n in imports(PACKAGE / "service.py") if starts(n, ("rag.llm", "rag.sqlmodel"))
    ]
