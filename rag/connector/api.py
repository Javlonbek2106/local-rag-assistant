"""The connector's HTTP surface: /api/v1/connections (docs/connector.md).

Every route needs a session: `principal` is a dependency that rag/api.py supplies (the authentication layer of M4.1a;
this package does not import it). The owner of everything is that principal's id, never a field of a request: a body
with an `owner_principal_id` (or any other field not listed) is refused.

Creating a connection, and changing where its credential goes (host, port, database, user, provider, TLS, the password
itself), needs a fresh step-up confirmation (`step_up`): a stolen session cannot aim the stored credential elsewhere.

Answers never contain a credential. Errors are {"error": {"code": ...}}: a fixed code, never text from a driver, and
even a rejected body is answered without echoing what was sent (FastAPI's default 422 would repeat the password).
"""

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg.errors import DataError
from pydantic import BaseModel, ConfigDict, Field

from rag import config
from rag.connector import service as service_module
from rag.connector.bodylimit import BodyTooLarge, ConnectorBodyLimit
from rag.connector.drivers import CancelToken
from rag.connector.errors import ConnectorError

NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
PREFIX = "/api/v1/connections"
INT_MAX = 2**31 - 1  # numbers from a request that end up in SQL stay inside what the database accepts: 422, never a 500


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ConnectionCreate(_Body):
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=4000)
    provider: str = Field(max_length=20)
    host: str = Field(max_length=300)
    port: int | None = None
    database_name: str = Field(max_length=300)
    username: str = Field(max_length=300)
    password: str = Field(max_length=2000)
    ssl_mode: str | None = Field(default=None, max_length=20)
    options: dict[str, Any] | None = None


class ConnectionPatch(_Body):
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    provider: str | None = Field(default=None, max_length=20)
    host: str | None = Field(default=None, max_length=300)
    port: int | None = None
    database_name: str | None = Field(default=None, max_length=300)
    username: str | None = Field(default=None, max_length=300)
    password: str | None = Field(default=None, max_length=2000)
    ssl_mode: str | None = Field(default=None, max_length=20)
    options: dict[str, Any] | None = None


class SelectionBody(_Body):
    items: list[dict[str, Any]] = Field(max_length=config.CONNECTOR_MAX_SELECTION_ITEMS)
    base_revision: int | None = Field(default=None, ge=-INT_MAX, le=INT_MAX)


class AcknowledgeBody(_Body):
    version: int = Field(ge=-INT_MAX, le=INT_MAX)


class GenerateBody(_Body):
    question: str = Field(min_length=1, max_length=2000)


class Expected(_Body):
    """What the page saw when it validated: execution is refused if any of it has changed since."""

    snapshot_fingerprint: str | None = Field(default=None, max_length=128)
    selection_revision: int | None = Field(default=None, ge=0, le=INT_MAX)
    target: str | None = Field(default=None, max_length=64)


class ValidateBody(_Body):
    sql: str = Field(
        min_length=1, max_length=100000
    )  # (the real limit is RAG_SQL_INPUT_MAX_CHARS, checked by the service)


class ExecuteBody(_Body):
    sql: str = Field(min_length=1, max_length=100000)
    max_rows: int | None = Field(default=None, ge=1, le=INT_MAX)
    expected: Expected | None = None


class DescriptionBody(_Body):
    target_kind: str = Field(max_length=20)
    schema_name: str = Field(default="", max_length=300)
    object_name: str = Field(default="", max_length=300)
    column_name: str = Field(default="", max_length=300)
    description: str = Field(default="", max_length=8000)


def connector_error_response(_request, error):
    body = {"code": error.code}
    if error.reason:
        body["reason"] = error.reason
    if error.details:
        body["details"] = error.details
    return JSONResponse({"error": body}, status_code=error.status, headers=NO_STORE)


async def validation_error_response(request, error):
    """On the connector's paths a malformed body is answered by field NAME only: the default answer repeats the value
    that was sent, and one of the fields is a password."""
    if not request.url.path.startswith(PREFIX):
        return await request_validation_exception_handler(request, error)
    fields = sorted({str(item["loc"][1]) for item in error.errors() if len(item.get("loc", ())) > 1})
    return JSONResponse({"error": {"code": "invalid_request", "fields": fields}}, status_code=422, headers=NO_STORE)


async def run_in_thread(_request, work, cancel=None):
    """The default way to run blocking work (the generation, an execution): in a worker thread, without a way to stop it
    when the client goes away. rag/connector_wiring.py hands in a cancellable runner instead."""
    return await asyncio.to_thread(work)


def build_router(service, principal, step_up, runner=run_in_thread):
    """`principal(request)` returns an object with `.id` or raises; `step_up(request)` returns or raises. Both come from
    rag/api.py. `runner(request, work)` runs the blocking SQL generation and returns None if the client went away."""

    def no_store(response: Response):
        response.headers.update(NO_STORE)

    router = APIRouter(prefix=PREFIX, dependencies=[Depends(no_store)])
    Owner = Annotated[Any, Depends(principal)]  # noqa: N806
    Confirmed = Annotated[Any, Depends(step_up)]  # noqa: N806

    @router.post("", status_code=201)
    def create(body: ConnectionCreate, who: Owner, _confirmed: Confirmed):
        return service.create(who.id, body.model_dump(exclude_none=True))

    @router.get("")
    def list_connections(who: Owner):
        return {"connections": service.list(who.id)}

    @router.get("/{connection_id}")
    def get(connection_id: str, who: Owner):
        return service.get(who.id, connection_id)

    @router.patch("/{connection_id}")
    def update(connection_id: str, body: ConnectionPatch, request: Request, who: Owner):
        changes = body.model_dump(exclude_unset=True)
        changes = {key: value for key, value in changes.items() if value is not None or key == "options"}
        if service_module.needs_step_up(changes):
            step_up(request)
        return service.update(who.id, connection_id, changes)

    @router.delete("/{connection_id}")
    def delete(connection_id: str, who: Owner):
        service.delete(who.id, connection_id)
        return {"deleted": True}

    @router.post("/{connection_id}/enable")
    def enable(connection_id: str, who: Owner):
        return service.set_enabled(who.id, connection_id, True)

    @router.post("/{connection_id}/disable")
    def disable(connection_id: str, who: Owner):
        return service.set_enabled(who.id, connection_id, False)

    @router.post("/{connection_id}/test")
    def test(connection_id: str, who: Owner):
        return service.test(who.id, connection_id)

    @router.post("/{connection_id}/schema/discover")
    def discover(connection_id: str, who: Owner):
        return service.discover(who.id, connection_id)

    @router.get("/{connection_id}/schema")
    def schemas(connection_id: str, who: Owner):
        return {"schemas": service.schemas(who.id, connection_id)}

    @router.get("/{connection_id}/schema/objects")
    def objects(
        connection_id: str,
        who: Owner,
        schema: str | None = None,
        q: str | None = Query(None, max_length=200),
        limit: int = Query(100, ge=-INT_MAX, le=INT_MAX),
        offset: int = Query(0, ge=-INT_MAX, le=INT_MAX),
    ):
        rows, total = service.objects(who.id, connection_id, schema, q, limit, offset)
        return {"objects": rows, "total": total}

    @router.get("/{connection_id}/schema/object")
    def object_detail(connection_id: str, schema: str, name: str, who: Owner):
        return service.object_detail(who.id, connection_id, schema, name)

    @router.get("/{connection_id}/schema/relationships")
    def relationships(
        connection_id: str,
        who: Owner,
        limit: int = Query(500, ge=-INT_MAX, le=INT_MAX),
        offset: int = Query(0, ge=-INT_MAX, le=INT_MAX),
    ):
        return {"relationships": service.relationships(who.id, connection_id, limit, offset)}

    @router.get("/{connection_id}/descriptions")
    @router.get("/{connection_id}/schema/descriptions")
    def descriptions(connection_id: str, who: Owner):
        return {"descriptions": service.descriptions(who.id, connection_id)}

    @router.get("/{connection_id}/schema/snapshots")
    def snapshots(connection_id: str, who: Owner):
        return {"snapshots": service.snapshots(who.id, connection_id)}

    @router.get("/{connection_id}/schema/snapshots/{snapshot_id}")
    def snapshot(connection_id: str, snapshot_id: str, who: Owner):
        return service.snapshot(who.id, connection_id, snapshot_id)

    @router.get("/{connection_id}/schema/changes")
    def changes(
        connection_id: str,
        who: Owner,
        version: int | None = Query(None, ge=-INT_MAX, le=INT_MAX),
        since_version: int | None = Query(None, ge=-INT_MAX, le=INT_MAX),
        object_type: str | None = Query(None, max_length=20),
        change_type: str | None = Query(None, max_length=20),
        limit: int = Query(200, ge=-INT_MAX, le=INT_MAX),
        offset: int = Query(0, ge=-INT_MAX, le=INT_MAX),
    ):
        return service.changes(
            who.id,
            connection_id,
            version=version,
            since_version=since_version,
            object_type=object_type,
            change_type=change_type,
            limit=limit,
            offset=offset,
        )

    @router.post("/{connection_id}/schema/changes/acknowledge")
    def acknowledge(connection_id: str, body: AcknowledgeBody, who: Owner):
        return service.acknowledge(who.id, connection_id, body.version)

    @router.get("/{connection_id}/schema/freshness")
    def freshness(connection_id: str, who: Owner):
        return service.freshness(who.id, connection_id)

    @router.get("/{connection_id}/schema/selections")
    def selections(connection_id: str, who: Owner):
        return service.selections(who.id, connection_id)

    @router.put("/{connection_id}/schema/selections")
    def put_selections(connection_id: str, body: SelectionBody, who: Owner):
        return service.put_selections(who.id, connection_id, body.items, body.base_revision)

    @router.get("/{connection_id}/schema/context")
    def context(
        connection_id: str,
        who: Owner,
        max_chars: int | None = Query(None, ge=-INT_MAX, le=INT_MAX),
        allow_stale: bool = False,
    ):
        return service.context(who.id, connection_id, max_chars, allow_stale)

    @router.post("/{connection_id}/sql/generate")
    async def generate_sql(connection_id: str, body: GenerateBody, request: Request, who: Owner):
        """A PROPOSED SQL query for a question, from the selected and current schema context (M4.3). The query is not
        checked and not run (`validation: "not_performed"`, `executed: false`); that is M4.4."""
        outcome = await runner(request, lambda: service.generate_sql(who.id, connection_id, body.question))
        if outcome is None:  # the client hung up: nobody is waiting for an answer
            return Response(status_code=499)
        return outcome

    @router.post("/{connection_id}/sql/validate")
    def validate_sql(connection_id: str, body: ValidateBody, who: Owner):
        """Parse and check a SQL text against this connection's selected, current schema (M4.4). Runs nothing and
        does not contact the external database. `execution_allowed` is a fact about this moment, not a permission."""
        return service.validate_sql(who.id, connection_id, body.sql)

    @router.post("/{connection_id}/sql/execute")
    async def execute_sql(connection_id: str, body: ExecuteBody, request: Request, who: Owner):
        """Validate again, from scratch, and run the canonical text read-only with bounded results (M4.4). Nothing the
        page says about an earlier validation is trusted; `expected` only lets the page ask to be refused if what it saw
        has changed."""
        cancel = CancelToken()
        expected = body.expected.model_dump(exclude_none=True) if body.expected else {}
        outcome = await runner(
            request,
            lambda: service.execute_sql(who.id, connection_id, body.sql, expected, body.max_rows, cancel),
            cancel,
        )
        if outcome is None:
            return Response(status_code=499)
        return outcome

    @router.put("/{connection_id}/descriptions")
    def describe(connection_id: str, body: DescriptionBody, who: Owner):
        return service.describe(
            who.id,
            connection_id,
            body.target_kind,
            body.schema_name,
            body.object_name,
            body.column_name,
            body.description,
        )

    return router


def body_too_large_response(_request, _error):
    return JSONResponse({"error": {"code": "request_too_large"}}, status_code=413, headers=NO_STORE)


def data_error_response(request, error):
    """The database refused a VALUE a request carried (a NUL byte in a name, a number out of range, a text that is not
    valid): the request was wrong, not the server. On the connector's paths that is a 400 in the connector's envelope,
    not a 500; anywhere else the error goes on as it did before (the handler is registered application-wide)."""
    if not request.url.path.startswith(PREFIX):
        raise error
    return JSONResponse({"error": {"code": "invalid_request"}}, status_code=400, headers=NO_STORE)


def install(app, service, principal, step_up, runner=run_in_thread):
    app.add_middleware(
        ConnectorBodyLimit
    )  # bodies are bounded as they arrive, before FastAPI reads them (bodylimit.py)
    app.add_exception_handler(BodyTooLarge, body_too_large_response)
    app.add_exception_handler(DataError, data_error_response)
    app.add_exception_handler(ConnectorError, connector_error_response)
    app.add_exception_handler(RequestValidationError, validation_error_response)
    app.include_router(build_router(service, principal, step_up, runner))
