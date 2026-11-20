"""What the connector does, in the order it must be done (the registry of 6a.7): create, change, test, discover,
describe.

Every method takes `owner` first: the id of the authenticated principal, handed in by rag/api.py, never read from a
request. Nothing here returns a credential, and nothing here lets a driver's own text out: a failure is a
ConnectorError or a DriverError with a fixed code.

Security-relevant changes (a connection made, changed, enabled, disabled, deleted, tested, discovered) are written to
the security log (`events`). A change that cannot be recorded is not made: changes of existing connections are
recorded first and done after; a new connection is made and then recorded, and removed again if the record fails. A
test or a discovery (which changes no configuration) is recorded with its outcome.
"""

import datetime
import logging
import re
import threading
import time
import uuid

from rag import config
from rag.connector import context as context_builder
from rag.connector import freshness as freshness_model
from rag.connector import network, repository, selection, sqlgen, validation
from rag.connector import snapshots as snap
from rag.connector.drivers import Target as DriverTarget
from rag.connector.drivers import driver as load_driver
from rag.connector.errors import DRIVER_CODES, WARNING_CODES, ConnectorError, DriverError, ModelError
from rag.connector.vault import Vault, VaultError

log = logging.getLogger(__name__)

# How a failure of the model is answered: (code, HTTP status). The text of the model server is never passed on.
MODEL_FAILURES = {
    "llm_timeout": ("generation_timeout", 504),
    "llm_unavailable": ("generation_unavailable", 503),
    "llm_error": ("generation_failed", 502),
    "llm_cancelled": ("generation_cancelled", 499),
}

# What a person may change about WHERE the credential goes. Changing any of it, or the password, needs a fresh step-up
# confirmation: otherwise a stolen session could point the stored credential at a server of its own.
TARGET_KEYS = frozenset({"provider", "host", "port", "database_name", "username", "ssl_mode", "options", "password"})
SCHEMA_PAGE_MAX = 500
DESCRIPTION_KINDS = ("database", "schema", "table", "column")
CHANGE_OBJECT_TYPES = ("schema", "table", "view", "column", "constraint", "relationship", "index")
CHANGE_TYPES = ("added", "removed", "modified")
# What a description must not look like: a connection string with a password in it, or a `password=...` setting. (A
# heuristic: it cannot recognise every secret, and it also refuses the password of THIS connection, exactly.)
SECRET_LIKE = re.compile(
    r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@|\b(password|passwd|pwd|secret|token|api[_-]?key)\s*=\s*\S+"
)


def needs_step_up(changes):
    return bool(set(changes) & TARGET_KEYS)


class Service:
    def __init__(
        self,
        vault: Vault,
        events,
        resolver=None,
        internal=None,
        driver_lookup=load_driver,
        sql_completer=None,
        clock=None,
    ):
        # `sql_completer(messages) -> sqlgen.Completion` is the language model (rag/sqlmodel.py gives it); without one,
        # generation answers `generation_unavailable`. The connector package imports no model code itself.
        self._vault, self._events = vault, events
        self._sql_completer = sql_completer
        self._today = clock or (lambda: datetime.datetime.now(datetime.timezone.utc).date())
        self._sql_slots = threading.BoundedSemaphore(max(1, config.SQL_MAX_CONCURRENT))
        self._network = {key: value for key, value in (("resolver", resolver), ("internal", internal)) if value}
        self._driver = driver_lookup
        self._slots = threading.BoundedSemaphore(max(1, config.CONNECTOR_MAX_CONCURRENT))
        self._busy, self._busy_lock = set(), threading.Lock()

    # ----- what is shown -----

    def _view(self, row, key_ids):
        options = dict(row["options"] or {})
        has_ca = bool(options.pop("ca_certificate_pem", None))
        if not row["is_enabled"]:
            status = "disabled"
        elif row["credential_key_id"] not in key_ids:
            status = "needs_credentials"
        else:
            status = "ready"
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "provider": row["provider"],
            "host": row["host"],
            "port": row["port"],
            "database_name": row["database_name"],
            "username": row["username"],
            "ssl_mode": row["ssl_mode"],
            "options": options,
            "ca_certificate_set": has_ca,
            "is_enabled": row["is_enabled"],
            "status": status,
            "credential_stored": True,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_tested_at": row["last_tested_at"],
            "last_test_status": row["last_test_status"],
            "last_test_error": row["last_test_error"],
            "last_test_warning": row["last_test_warning"],
            "schema_discovered_at": row["schema_discovered_at"],
            "last_discovery_at": row["last_discovery_at"],
            "last_discovery_status": row["last_discovery_status"],
            "last_discovery_error": row["last_discovery_error"],
        }

    def _key_ids(self):
        try:
            return self._vault.key_ids()
        except VaultError:
            return frozenset()

    # ----- the security log -----

    def _log_must_work(self):
        if not self._events.check():
            raise ConnectorError("security_log_unavailable", 503)

    def _record(self, event, reason, owner, connection_id, undo=None):
        if not self._events.emit(event, reason, principal_id=owner, event_ref=connection_id):
            if undo:
                undo()
            raise ConnectorError("security_log_unavailable", 503)

    # ----- connections -----

    def create(self, owner, payload):
        fields = validation.connection_fields(payload)
        secret = validation.password(payload.get("password"))
        target = self._judge(fields)
        validation.tls_policy(fields, target.loopback)
        if repository.count(owner) >= config.CONNECTOR_MAX_CONNECTIONS:
            raise ConnectorError("limit_reached", 409)
        self._log_must_work()
        connection_id = str(uuid.uuid4())
        sealed = self._seal(secret, connection_id)
        row = repository.insert(owner, connection_id, fields, sealed)
        self._record(
            "connection_created", "ok", owner, connection_id, undo=lambda: repository.delete(owner, connection_id)
        )
        return self._view(row, self._key_ids())

    def list(self, owner):
        key_ids = self._key_ids()
        return [self._view(row, key_ids) for row in repository.list_connections(owner)]

    def get(self, owner, connection_id):
        return self._view(self._row(owner, connection_id), self._key_ids())

    def update(self, owner, connection_id, changes):
        """`changes`: any of the editable fields and/or `password`. A field not given keeps its value; a password not
        given keeps the stored (encrypted) one. The caller (the API) has already required a step-up if
        `needs_step_up(changes)`."""
        row = self._row(owner, connection_id)
        unknown = set(changes) - set(repository.EDITABLE) - {"password"}
        if unknown:
            raise ConnectorError("invalid_options")
        merged = {key: row[key] for key in repository.EDITABLE}
        merged.update({key: changes[key] for key in changes if key != "password"})
        if "provider" in changes and "port" not in changes:
            merged["port"] = validation.PROVIDERS.get(changes["provider"], row["port"])
        if "options" in changes and isinstance(changes["options"], dict):
            old_ca = (row["options"] or {}).get("ca_certificate_pem")
            if old_ca and "ca_certificate_pem" not in changes["options"]:
                merged["options"] = {**changes["options"], "ca_certificate_pem": old_ca}  # a stored CA is kept
        fields = validation.connection_fields(merged)
        sealed = None
        if "password" in changes:
            sealed = self._seal(validation.password(changes["password"]), connection_id)
        changed = {key: fields[key] for key in repository.EDITABLE if fields[key] != row[key]}
        if needs_step_up(changed) or sealed is not None:  # where the credential goes is judged again; a rename is not
            validation.tls_policy(fields, self._judge(fields).loopback)
        if not changed and sealed is None:
            return self._view(row, self._key_ids())
        self._not_busy(connection_id)
        self._log_must_work()
        reason = "credential" if needs_step_up(changed) or sealed else "ok"
        self._record("connection_updated", reason, owner, connection_id)  # BEFORE it is done: unrecorded = not made
        updated = repository.update(owner, connection_id, changed, sealed)
        if updated is None:
            raise ConnectorError("not_found", 404)
        return self._view(updated, self._key_ids())

    def set_enabled(self, owner, connection_id, enabled):
        self._row(owner, connection_id)
        self._not_busy(connection_id)
        self._log_must_work()
        self._record("connection_enabled" if enabled else "connection_disabled", "ok", owner, connection_id)
        if not repository.set_enabled(owner, connection_id, enabled):
            raise ConnectorError("not_found", 404)
        return self.get(owner, connection_id)

    def delete(self, owner, connection_id):
        self._row(owner, connection_id)
        self._not_busy(connection_id)
        self._log_must_work()
        self._record("connection_deleted", "ok", owner, connection_id)
        if not repository.delete(owner, connection_id):
            raise ConnectorError("not_found", 404)

    # ----- the external database -----

    def test(self, owner, connection_id):
        """Connect, authenticate, SELECT 1, look at the account's rights. The result (and only a fixed code on failure)
        is stored. Refused for a disabled connection or one whose credential cannot be opened."""
        with self._external(owner, connection_id) as (row, target, password):
            self._log_must_work()
            timeout = min(
                float(row["options"].get("connect_timeout_s", config.CONNECTOR_TEST_TIMEOUT_S)),
                config.CONNECTOR_TEST_TIMEOUT_S,
            )
            try:
                found = self._driver(row["provider"]).test(target, password, timeout)
                status, error = "ok", None
                warning = "account_can_write" if found.can_write else None
                version = found.version
            except DriverError as failure:
                status, error, warning, version = "failed", failure.code, None, None
            except Exception:  # noqa: BLE001 - a driver that raises anything else: no text from it, one fixed code
                status, error, warning, version = "failed", "failed", None, None
        assert error is None or error in DRIVER_CODES
        assert warning is None or warning in WARNING_CODES
        repository.record_test(owner, connection_id, status, error, warning)
        self._record("connection_tested", status, owner, connection_id)
        return {"status": status, "error": error, "warning": warning, "server_version": version}

    def discover(self, owner, connection_id):
        """Read the catalog of the external database into the internal one (names, types, keys: no rows) as a new
        snapshot, or confirm the current one if nothing changed. A failure or a cut-off leaves the previous snapshot as
        it was, and is remembered separately (freshness `failed`)."""
        with self._external(owner, connection_id) as (row, target, password):
            self._log_must_work()
            snap.begin_discovery(owner, connection_id)
            try:
                with repository.schema_replacement(
                    owner, connection_id, config.CONNECTOR_MAX_OBJECTS, config.CONNECTOR_MAX_COLUMNS
                ) as writer:
                    self._driver(row["provider"]).introspect(
                        target, password, writer, config.CONNECTOR_DISCOVERY_TIMEOUT_S
                    )
                    counts = writer.counts()
                result = writer.result
            except ConnectorError:
                raise
            except Exception as failure:  # noqa: BLE001 - a DriverError, or anything else: only a fixed code goes on
                reason = failure.code if isinstance(failure, DriverError) else "failed"
                snap.record_failure(owner, connection_id, reason)
                self._record("schema_discovered", "failed", owner, connection_id)
                raise ConnectorError("discovery_failed", 502, reason=reason) from None
        self._record("schema_discovered", "ok", owner, connection_id)
        return {
            **counts,
            "schema_discovered_at": repository.get(owner, connection_id)["schema_discovered_at"],
            "snapshot": {
                "id": result["snapshot_id"],
                "version": result["version"],
                "fingerprint": result["fingerprint"],
                "changed": result["changed"],
                "first": result["first"],
                "changes": result["changes"],
                "changes_truncated": result["changes_truncated"],
            },
        }

    # ----- discovered schema and descriptions -----

    def schemas(self, owner, connection_id):
        self._row(owner, connection_id)
        return repository.schemas(owner, connection_id)

    def objects(self, owner, connection_id, schema=None, query=None, limit=100, offset=0):
        self._row(owner, connection_id)
        return repository.objects(
            owner, connection_id, schema, query, min(max(limit, 1), SCHEMA_PAGE_MAX), max(offset, 0)
        )

    def object_detail(self, owner, connection_id, schema, name):
        self._row(owner, connection_id)
        found = repository.object_detail(owner, connection_id, schema, name)
        if found is None:
            raise ConnectorError("object_not_found", 404)
        return found

    def relationships(self, owner, connection_id, limit=500, offset=0):
        self._row(owner, connection_id)
        return repository.relationships(owner, connection_id, min(max(limit, 1), SCHEMA_PAGE_MAX), max(offset, 0))

    def descriptions(self, owner, connection_id):
        self._row(owner, connection_id)
        return repository.descriptions(owner, connection_id)

    def describe(self, owner, connection_id, kind, schema, name, column, text):
        """Set (or, with empty text, remove) the description of the database, a schema, a table/view or a column."""
        self._row(owner, connection_id)
        if kind not in DESCRIPTION_KINDS:
            raise ConnectorError("invalid_target")
        parts = {
            "database": ("", "", ""),
            "schema": (schema, "", ""),
            "table": (schema, name, ""),
            "column": (schema, name, column),
        }[kind]
        if kind != "database" and not all(parts[: {"schema": 1, "table": 2, "column": 3}[kind]]):
            raise ConnectorError("invalid_target")
        text = validation.description(text) if text else ""
        self._not_busy(connection_id)
        if text and self._looks_secret(owner, connection_id, text):
            raise ConnectorError("description_contains_secret", 400)
        if text and not repository.target_exists(owner, connection_id, kind, *parts):
            raise ConnectorError("object_not_found", 404)
        if not repository.set_description(owner, connection_id, kind, *parts, text):
            raise ConnectorError("not_found", 404)
        return {
            "target_kind": kind,
            "schema_name": parts[0],
            "object_name": parts[1],
            "column_name": parts[2],
            "description": text,
        }

    # ----- snapshots, changes, freshness -----

    def snapshots(self, owner, connection_id):
        self._row(owner, connection_id)
        return snap.list_snapshots(owner, connection_id)

    def snapshot(self, owner, connection_id, snapshot_id):
        self._row(owner, connection_id)
        try:
            uuid.UUID(str(snapshot_id))
        except ValueError:
            raise ConnectorError("snapshot_not_found", 404) from None
        found = snap.get_snapshot(owner, connection_id, snapshot_id)
        if found is None:
            raise ConnectorError("snapshot_not_found", 404)
        return found

    def changes(
        self,
        owner,
        connection_id,
        *,
        version=None,
        since_version=None,
        object_type=None,
        change_type=None,
        limit=200,
        offset=0,
    ):
        self._row(owner, connection_id)
        if (object_type and object_type not in CHANGE_OBJECT_TYPES) or (
            change_type and change_type not in CHANGE_TYPES
        ):
            raise ConnectorError("invalid_request")
        rows, total, summary = snap.list_changes(
            owner,
            connection_id,
            version=version,
            since_version=since_version,
            object_type=object_type,
            change_type=change_type,
            limit=min(max(limit, 1), SCHEMA_PAGE_MAX),
            offset=max(offset, 0),
        )
        return {"changes": rows, "total": total, "summary": summary}

    def _stored_selection(self, owner, connection_id):
        found = snap.get_selection(owner, connection_id)
        if found is None:
            raise ConnectorError("not_found", 404)
        revision, items = found
        return revision, items, selection.Selection(items)

    def _key_columns(self, owner, connection_id, row, tables):
        """The primary and foreign key columns of some tables of the current snapshot: {(schema, name): {columns}}."""
        source = snap.SnapshotSource(owner, connection_id, row, None, {})
        keys = {}
        for obj, items in source.constraints(set(tables)).items():
            for c in items:
                if c["kind"] in ("primary_key", "foreign_key"):
                    keys.setdefault(obj, set()).update(c["columns"])
        return keys

    def _freshness(self, owner, connection_id, row, chosen, items):
        facts = snap.facts(owner, connection_id)
        has_snapshot = facts["snapshot_id"] is not None
        recorded = snap.changes_since(owner, connection_id, facts["context_confirmed_version"]) if has_snapshot else []
        partial = {
            (c["schema_name"], c["object_name"])
            for c in recorded
            if c["object_type"] == "column"
            and (chosen.state(c["schema_name"], c["object_name"]) or ("",))[0] == "columns"
        }
        keys = self._key_columns(owner, connection_id, row, partial) if partial else {}
        affected = sum(
            1
            for c in recorded
            if selection.affects(chosen, c, frozenset(keys.get((c["schema_name"], c["object_name"]), ())))
        )
        gone = sum(1 for _item, ok in self._availability(owner, connection_id, chosen) if not ok) if has_snapshot else 0
        orphaned = sum(1 for d in repository.descriptions(owner, connection_id) if d["orphaned"]) if has_snapshot else 0
        state = freshness_model.compute(
            facts["now"],
            has_snapshot=has_snapshot,
            verified_at=facts["verified_at"],
            discovery_started_at=facts["discovery_started_at"],
            last_status=facts["last_discovery_status"],
            last_error=facts["last_discovery_error"],
            last_attempt_at=facts["last_discovery_at"],
            lease_s=2 * config.CONNECTOR_DISCOVERY_TIMEOUT_S + 30,
            max_age_s=config.CONNECTOR_SCHEMA_MAX_AGE_S,
            unreviewed_changes=len(recorded),
            affected_selected=affected,
            removed_selected=gone,
            orphaned_descriptions=orphaned,
            target_changed=has_snapshot
            and (facts["snapshot_provider"], facts["snapshot_database"]) != (row["provider"], row["database_name"]),
        )
        snapshot = None
        if has_snapshot:
            snapshot = {
                "id": str(facts["snapshot_id"]),
                "version": facts["version"],
                "fingerprint": facts["fingerprint"],
                "created_at": facts["created_at"],
                "verified_at": facts["verified_at"],
                "objects": facts["objects_count"],
                "columns": facts["columns_count"],
            }
        return {**state, "snapshot": snapshot, "confirmed_version": facts["context_confirmed_version"]}

    def freshness(self, owner, connection_id):
        row = self._row(owner, connection_id)
        _revision, items, chosen = self._stored_selection(owner, connection_id)
        return self._freshness(owner, connection_id, row, chosen, items)

    def acknowledge(self, owner, connection_id, version):
        """The owner has seen the changes up to snapshot `version` (which must be the current one)."""
        self._row(owner, connection_id)
        if isinstance(version, bool) or not isinstance(version, int):
            raise ConnectorError("invalid_request")
        self._not_busy(connection_id)
        self._log_must_work()
        self._record("schema_changes_acknowledged", "ok", owner, connection_id)
        snap.acknowledge(owner, connection_id, version)
        return self.freshness(owner, connection_id)

    # ----- selection -----

    def _availability(self, owner, connection_id, chosen):
        """[(item, exists in the current snapshot)] for the stored items."""
        found = snap.existing(owner, connection_id, chosen.items)
        return [(i, (i["kind"], i["schema"], i["object"], i["column"]) in found) for i in chosen.items]

    def selections(self, owner, connection_id):
        self._row(owner, connection_id)
        revision, items, chosen = self._stored_selection(owner, connection_id)
        listed = [{**i, "available": ok} for i, ok in self._availability(owner, connection_id, chosen)]
        return {"revision": revision, "items": listed, "unavailable": sum(1 for i in listed if not i["available"])}

    def put_selections(self, owner, connection_id, items, base_revision=None):
        """Replace the selection (the whole of it). New items must name something in the current snapshot; items that
        are already stored are kept even if their target is gone (they stay, shown as unavailable)."""
        self._row(owner, connection_id)
        if base_revision is not None and (isinstance(base_revision, bool) or not isinstance(base_revision, int)):
            raise ConnectorError("invalid_request")
        clean = selection.normalise(items)

        def key(i):
            return (i["kind"], i["schema"], i["object"], i["column"])

        def validate(stored):
            known = {key(i) for i in stored}
            fresh_items = [i for i in clean if key(i) not in known]
            found = snap.existing(owner, connection_id, fresh_items)
            if [i for i in fresh_items if key(i) not in found]:
                raise ConnectorError("object_not_found", 404, reason="not_in_snapshot")

        self._not_busy(connection_id)
        self._log_must_work()
        self._record("schema_selection_changed", "ok", owner, connection_id)
        revision = snap.put_selection(owner, connection_id, clean, base_revision, validate)
        return {"revision": revision, "items": len(clean)}

    # ----- the context -----

    def context(self, owner, connection_id, max_chars=None, allow_stale=False):
        """The schema context of the selection, as the context builder (context.py) makes it. Without `allow_stale` a
        context that is not current is refused (409 context_stale): a caller that is going to use it for something must
        not do so by accident."""
        row = self._row(owner, connection_id)
        if not row["is_enabled"]:
            raise ConnectorError("connection_disabled", 409)
        if max_chars is not None and (
            isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 200 <= max_chars
        ):
            raise ConnectorError("invalid_request")
        revision, items, chosen = self._stored_selection(owner, connection_id)
        fresh = self._freshness(owner, connection_id, row, chosen, items)
        if fresh["state"] != freshness_model.CURRENT and not allow_stale:
            raise ConnectorError("context_stale", 409, reason=fresh["state"])
        snapshot = None
        if fresh["snapshot"]:
            snapshot = {
                "version": fresh["snapshot"]["version"],
                "fingerprint": fresh["snapshot"]["fingerprint"],
                "verified_at": fresh["snapshot"]["verified_at"],
            }
        described = {}
        for d in repository.descriptions(owner, connection_id):
            if not d["orphaned"]:
                described[
                    {
                        "database": ("database",),
                        "schema": ("schema", d["schema_name"]),
                        "table": ("table", d["schema_name"], d["object_name"]),
                        "column": ("column", d["schema_name"], d["object_name"], d["column_name"]),
                    }[d["target_kind"]]
                ] = d["description"]
        source = snap.SnapshotSource(owner, connection_id, row, snapshot, described)
        return context_builder.build(
            source,
            chosen,
            {k: v for k, v in fresh.items() if k != "snapshot"},
            max_chars=max_chars,
            revision=revision,
            unavailable=self._availability(owner, connection_id, chosen) if snapshot else (),
        )

    # ----- natural language -> SQL (M4.3): a PROPOSED query; nothing is validated, nothing is run -----

    def generate_sql(self, owner, connection_id, question):
        """Turn a question into a proposed SQL query using only the selected, current schema context of this connection.

        The result is TEXT: it has not been checked (no parsing, no check that the tables and columns exist, no check
        that it is a SELECT: M4.4) and it is never executed; the external database is not contacted at all (the schema
        comes from the snapshot in the internal database). The order matters: the owner and the connection are proved
        first (404 for anything else), then the context must be usable, and only then is the model asked."""
        question = self._question(question)
        row = self._row(owner, connection_id)
        if not row["is_enabled"]:
            raise ConnectorError("connection_disabled", 409)
        if self._sql_completer is None:
            raise ConnectorError("generation_unavailable", 503, reason="not_configured")
        dialect = sqlgen.dialect_of(row["provider"])
        today = self._today()
        budget = sqlgen.context_budget(row["provider"], today, question)
        if not self._sql_slots.acquire(blocking=False):
            raise ConnectorError("busy", 429)
        request_id = str(uuid.uuid4())
        started = time.monotonic()
        try:
            shown = self.context(owner, connection_id, max_chars=budget)  # stale or missing: context_stale (409)
            if shown["status"] == "empty":
                raise ConnectorError("context_empty", 409)
            if not shown["usable"]:
                raise ConnectorError("context_unusable", 409, reason=shown["status"])
            msgs = sqlgen.messages(row["provider"], today, question, sqlgen.schema_json(shown["context"]))
            sqlgen.check_prompt(msgs)
            try:
                completion = self._sql_completer(msgs)
            except ModelError as failure:
                code, status = MODEL_FAILURES[failure.code]
                raise ConnectorError(code, status, reason=failure.code) from None
            except ConnectorError:
                raise
            except Exception as failure:  # noqa: BLE001 - nothing of the model's own text goes on: only a fixed code
                log.warning("sql generation: the model call failed unexpectedly (%s)", type(failure).__name__)
                raise ConnectorError("generation_failed", 502, reason="llm_error") from None
            if completion.truncated:
                raise ConnectorError("model_output_truncated", 502)
            answer = sqlgen.parse(completion.text)
        except ConnectorError as refused:
            log.warning(
                "sql generation refused: request=%s connection=%s code=%s", request_id, connection_id, refused.code
            )
            raise
        finally:
            self._sql_slots.release()
        elapsed = int((time.monotonic() - started) * 1000)
        chars = sqlgen.prompt_chars(msgs)
        log.info(
            "sql generation: request=%s connection=%s status=%s prompt_chars=%d ms=%d",
            request_id,
            connection_id,
            answer["status"],
            chars,
            elapsed,
        )
        selected, spent, snapshot, fresh = shown["selection"], shown["budget"], shown["snapshot"], shown["freshness"]
        return {
            "request_id": request_id,
            "connection_id": str(connection_id),
            "provider": row["provider"],
            "dialect": dialect,
            **answer,
            # what the caller must not overlook: the model saw only part of the selected schema
            "warnings": [] if shown["complete"] else ["context_incomplete"],
            "validation": "not_performed",  # M4.3 does not check the query: the caller must not treat it as checked
            "executed": False,  # and nothing runs it
            "model": completion.model,
            "prompt": {"template_version": sqlgen.TEMPLATE_VERSION, "chars": chars},
            "context": {
                "state": fresh["state"],
                "snapshot_version": snapshot["version"] if snapshot else None,
                "snapshot_verified_at": snapshot["verified_at"] if snapshot else None,
                "fingerprint": snapshot["fingerprint"] if snapshot else None,
                "selected_objects": selected["selected_objects"],
                "included_objects": selected["included_objects"],
                "complete": shown["complete"],
                "omitted_objects": shown["context"]["omitted"]["count"],
                "max_chars": spent["max_chars"],
                "used_chars": spent["used_chars"],
                "context_fingerprint": spent["context_fingerprint"],
            },
            "duration_ms": elapsed,
        }

    def _question(self, question):
        if not isinstance(question, str):
            raise ConnectorError("invalid_question", 400, reason="not_text")
        text = question.strip()
        if not text:
            raise ConnectorError("invalid_question", 400, reason="empty")
        if len(text) > sqlgen.QUESTION_MAX_CHARS:
            raise ConnectorError("invalid_question", 400, reason="too_long")
        if "\x00" in text:
            raise ConnectorError("invalid_question", 400, reason="bad_characters")
        return text

    # ----- helpers -----

    def _looks_secret(self, owner, connection_id, text):
        if SECRET_LIKE.search(text):
            return True
        try:
            password = self._vault.open(repository.get_sealed(owner, connection_id), connection_id)
        except VaultError:
            return False
        return len(password) >= 6 and password in text

    def _row(self, owner, connection_id):
        try:
            uuid.UUID(str(connection_id))
        except ValueError:
            raise ConnectorError("not_found", 404) from None
        row = repository.get(owner, connection_id)
        if row is None:
            raise ConnectorError("not_found", 404)
        return row

    def _not_busy(self, connection_id):
        """A test or a discovery is running on this connection: a change would wait for its database lock (up to a
        minute) holding one of the application's few pooled connections, so it is refused instead."""
        with self._busy_lock:
            if connection_id in self._busy:
                raise ConnectorError("busy", 409)

    def _seal(self, secret, connection_id):
        try:
            return self._vault.seal(secret, connection_id)
        except VaultError as failure:
            raise ConnectorError("key_unavailable", 503, reason=failure.code) from None

    def _judge(self, fields):
        return network.check_target(
            fields["host"], fields["port"], fields["database_name"], fields["username"], **self._network
        )

    def _external(self, owner, connection_id):
        return _External(self, owner, connection_id)


class _External:
    """A guard for one operation against an external database: the connection is enabled and its credential opens; at
    most CONNECTOR_MAX_CONCURRENT such operations run at once and never two on the same connection; the address is
    judged anew (a name may resolve differently than when the connection was made)."""

    def __init__(self, service, owner, connection_id):
        self._s, self._owner, self._id = service, owner, connection_id
        self._slot = self._claimed = False

    def __enter__(self):
        s = self._s
        row = s._row(self._owner, self._id)
        if not row["is_enabled"]:
            raise ConnectorError("connection_disabled", 409)
        sealed = repository.get_sealed(self._owner, self._id)
        try:
            password = s._vault.open(sealed, self._id)
        except VaultError:
            raise ConnectorError("needs_credentials", 409) from None
        fields = validation.connection_fields({key: row[key] for key in repository.EDITABLE})
        judged = s._judge(fields)
        validation.tls_policy(fields, judged.loopback)
        if not s._slots.acquire(blocking=False):
            raise ConnectorError("busy", 429)
        self._slot = True
        with s._busy_lock:
            if self._id in s._busy:
                self._release()
                raise ConnectorError("busy", 409)
            s._busy.add(self._id)
            self._claimed = True
        target = DriverTarget(
            judged.host,
            judged.addresses,
            judged.port,
            fields["database_name"],
            fields["username"],
            fields["ssl_mode"],
            row["options"].get("ca_certificate_pem"),
        )
        return row, target, password

    def _release(self):
        if self._claimed:
            with self._s._busy_lock:
                self._s._busy.discard(self._id)
            self._claimed = False
        if self._slot:
            self._s._slots.release()
            self._slot = False

    def __exit__(self, *exc):
        self._release()
        return False
