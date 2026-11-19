"""Bounded request bodies for the connector's routes (roadmap M4.2 security review).

Why this is a middleware and not a check inside the router: FastAPI reads and parses the whole body of a request BEFORE
it runs the dependencies of the route. A `Content-Length` check in a dependency therefore only runs after the body
(whatever size it had) was already in memory, and it never sees a body that has no `Content-Length` (a chunked one) or
one that is longer than it claimed. This plain ASGI middleware sits in front of the router, as `rag/bodylimit.py` does
for uploads, and:

- refuses a declared `Content-Length` over the limit at once, with 413, before a byte of the body is read;
- refuses a `Content-Length` that is not a plain non-negative number (or is given twice, differently) with 400;
- COUNTS the bytes that really arrive and refuses with 413 the moment the count passes the limit, whatever the header
  said or did not say (a chunked body has no header; a misleading one claims less than it sends). At most `limit` bytes
  are ever buffered.

The routes of the sign-in (`/api/v1/session/…`: a pairing code is a few dozen bytes) are bounded the same way, at
`MAX_SESSION_BODY_BYTES` (16 KB): they are reachable without a session, so they are the ones an anonymous caller can
send bodies to.

The limit of the connector's routes is `MAX_BODY_BYTES` (256 KB: a connection or a description is a few hundred
bytes, a CA certificate the largest part) for every route, and `MAX_SELECTION_BODY_BYTES` (8 MB: a selection can list
tens of thousands of items) only for `PUT …/schema/selections` of a request that at least carries a bearer token.
Whether the token is valid is decided later by the router; an anonymous caller does not get to make the server buffer
megabytes.
"""

import json

from fastapi import HTTPException

from rag.bodylimit import BadContentLength, declared_length

PREFIX = "/api/v1/connections"
SESSION_PREFIX = "/api/v1/session"  # the pairing code and the step-up code: a few dozen bytes of JSON
MAX_BODY_BYTES = 256 * 1024
MAX_SESSION_BODY_BYTES = 16 * 1024
MAX_SELECTION_BODY_BYTES = 8 * 1024 * 1024
BODYLESS = ("GET", "HEAD", "OPTIONS")
NO_STORE = [(b"cache-control", b"no-store"), (b"referrer-policy", b"no-referrer")]


class BodyTooLarge(HTTPException):
    """Raised while the body is being read; FastAPI passes an HTTPException through, and the connector's handler
    (api.py) turns this subclass into the connector's error envelope."""

    def __init__(self):
        super().__init__(413, "request_too_large")


def covered(path, prefix):
    return path == prefix or path.startswith(prefix + "/")


def limit_for(scope):
    path = scope["path"]
    if covered(path, SESSION_PREFIX):
        return MAX_SESSION_BODY_BYTES
    bearer = any(
        name == b"authorization" and value[:7].lower() == b"bearer " and len(value) > 7
        for name, value in scope["headers"]
    )
    if scope["method"] == "PUT" and bearer and path.startswith(PREFIX + "/") and path.endswith("/schema/selections"):
        return MAX_SELECTION_BODY_BYTES
    return MAX_BODY_BYTES


async def reject(send, status, code):
    body = json.dumps({"error": {"code": code}}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), *NO_STORE]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class ConnectorBodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if (
            scope["type"] != "http"
            or scope["method"] in BODYLESS
            or not (covered(path, PREFIX) or covered(path, SESSION_PREFIX))
        ):
            await self.app(scope, receive, send)
            return
        limit = limit_for(scope)
        try:
            declared = declared_length(scope["headers"])
        except BadContentLength:
            await reject(send, 400, "invalid_content_length")
            return
        if declared is not None and declared > limit:
            await reject(send, 413, "request_too_large")  # before a single byte of the body is read
            return
        received = 0

        async def counting_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise BodyTooLarge
            return message

        await self.app(scope, counting_receive, send)
