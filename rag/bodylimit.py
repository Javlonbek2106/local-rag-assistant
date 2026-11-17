"""Turn an upload body that is larger than the limit away as early as possible (roadmap T0.5, audit API-02).

Starlette reads a multipart body completely, into temporary files, *before* the upload handler runs, and only the
handler compared the size with the limit. A client could therefore fill the disk with any number of megabytes that
the limit was meant to refuse (measured in the audit: 150 MB sent against a 1 MB limit left 139 MB in the temporary
folder). This is a plain ASGI middleware in front of the router that closes that path:

- `Content-Length` larger than the limit: answered with 413 at once; the body is never read, so nothing is written
  (and a client that sent `Expect: 100-continue` never even sends it).
- No `Content-Length` (a chunked body): refused with 411 Length Required before a byte of the body is read. Without a
  declared size nothing can tell an oversized body from a legitimate one until the limit's worth of bytes has already
  been written to a temporary file (measured: about 195 MB for a 250 MB chunked body against the default limit), so the
  size has to be announced. Browsers always do it for a FormData upload (checked in Edge, also for a 150 MB file).
- A body longer than its `Content-Length` claimed (a server that hands the surplus over): the bytes that really arrive
  are counted, and the request is refused with 413 the moment the count passes the limit. Whatever was already written
  to a temporary file is closed and deleted by Starlette's own error handling (a test proves it).
- A `Content-Length` that is not a plain non-negative number is refused with 400; it cannot be used to slip past.

The refusal does not close the connection. Measured: with `Connection: close` a client that is still sending (a browser
uploading a big file) gets a TCP reset and never sees the 413; without it the server answers at once and reads and
discards the rest of the body (150 MB in about 0.13 s on this machine) without storing or buffering any of it.

The limit of one request is `config.MAX_UPLOAD_BYTES` (the size limit of one file, unchanged) plus UPLOAD_FRAMING_BYTES
for the multipart boundaries and headers, so a file of exactly the allowed size is still accepted and the per-file
message of the upload handler still applies to a file that is a little too large. It is read for every request, so
every upload (also parallel ones) is judged on its own. Only `POST /api/documents` is checked; no other endpoint is
touched. Streaming the file straight to its final place instead of spooling it first is roadmap T1.3.
"""
import json
import re

from fastapi import HTTPException

from rag import config

UPLOAD_PATH = "/api/documents"
DIGITS = re.compile(rb"[0-9]{1,18}")  # a plain length: no sign, no spaces, no exponent, no absurdly long number


def request_limit():
    """The most bytes one upload request may carry: the largest allowed file plus the multipart framing."""
    return config.MAX_UPLOAD_BYTES + config.UPLOAD_FRAMING_BYTES


def too_large():
    megabytes = max(1, config.MAX_UPLOAD_BYTES // 1024 // 1024)
    return HTTPException(413, f"Yuklash hajmi chegaradan katta: bitta so'rovda eng ko'pi {megabytes} MB")


class BadContentLength(Exception):
    pass


def declared_length(headers):
    """The `Content-Length` of the request: an int, None if there is none, or BadContentLength."""
    found = {value.strip() for name, value in headers if name == b"content-length"}
    if not found:
        return None
    if len(found) > 1 or not DIGITS.fullmatch(next(iter(found))):
        raise BadContentLength
    return int(next(iter(found)))


class UploadBodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != UPLOAD_PATH:
            await self.app(scope, receive, send)
            return
        limit = request_limit()
        try:
            declared = declared_length(scope["headers"])
        except BadContentLength:
            await reject(send, 400, "Content-Length noto'g'ri")
            return
        if declared is None:
            await reject(send, 411, "Content-Length sarlavhasi kerak: so'rov hajmi oldindan ma'lum bo'lishi shart")
            return
        if declared > limit:
            error = too_large()
            await reject(send, error.status_code, error.detail)  # before a single byte of the body is read
            return
        received = 0

        async def counting_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:  # the bytes that really arrive count, whatever the header said
                    raise too_large()  # becomes the 413 response; Starlette closes the temporary files on the way out
            return message

        await self.app(scope, counting_receive, send)


async def reject(send, status, detail):
    body = json.dumps({"detail": detail}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
