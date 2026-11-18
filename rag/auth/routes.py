"""The routes of the authentication layer: pairing, session, step-up, and the connector's status. All under /api/v1.

Added to the application only when the connector is enabled (decision D-15); otherwise these paths do not exist, except
one: GET /api/v1/connector, which says whether the connector is on. It is always there so that the page can ask without
being answered with a 404 (browsers write every 404 to the console). It opens nothing and changes no existing route.
The connector's own endpoints (a later milestone) will depend on `ctx.current_principal`.
"""
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from rag import config
from rag.auth.deps import SECURITY_HEADERS, AuthError, auth_error_response


class Code(BaseModel):
    code: str = Field(min_length=1, max_length=40)


class PairingRequest(BaseModel):
    purpose: str = Field(default="login", pattern="^(login|step-up)$")


def discovery(enabled):
    router = APIRouter(prefix="/api/v1")

    @router.get("/connector")
    def connector_enabled():
        return {"enabled": enabled}

    return router


def install(app, ctx):
    """Add the routes and the way their errors are answered to `app`."""
    app.add_exception_handler(AuthError, auth_error_response)
    app.include_router(discovery(True))
    app.include_router(build_router(ctx))


def install_discovery(app):
    """The connector is off: the one route that says so."""
    app.include_router(discovery(False))


def build_router(ctx):
    def no_store(response: Response):
        response.headers.update(SECURITY_HEADERS)

    router = APIRouter(prefix="/api/v1", dependencies=[Depends(no_store)])

    def view(session):
        left = ctx.sessions.remaining(session)
        return {"absolute_remaining_s": left["absolute"], "idle_remaining_s": left["idle"],
                "step_up_remaining_s": left["step_up"]}

    def granted(token, session):
        return {"token": token, "expires_in_s": ctx.sessions.remaining(session)["absolute"],
                "idle_timeout_s": config.SESSION_IDLE_S}

    def lost_event():
        """Access was about to be granted but the security event could not be recorded: it is not granted."""
        return AuthError(503, "security_log_unavailable")

    @router.post("/session/pairing")
    def request_code(request: Request, body: PairingRequest | None = None):
        """Ask for a one-time code. It is shown in the console of the application, never in the answer."""
        purpose = (body or PairingRequest()).purpose
        session_ref = None
        if purpose == "step-up":
            _principal, session, _token = ctx.authenticate(request)
            session_ref = session.ref
        elif not ctx.available():
            ctx.events.emit("pairing_requested", "unavailable")
            raise AuthError(503, "connector_unavailable")
        result = ctx.pairing.request(purpose=purpose, session=session_ref)
        ctx.events.emit("pairing_requested", result)
        if result == "unavailable":
            raise AuthError(503, "connector_unavailable")
        if result != "ok":
            raise AuthError(429, "rate_limited", {"Retry-After": str(max(config.PAIRING_REQUEST_GAP_S,
                                                                          ctx.pairing.retry_after()))})
        return {"requested": True, "ttl_s": config.PAIRING_TTL_S}

    @router.post("/session/exchange")
    def exchange(body: Code):
        """Exchange the code for a session. Every failure is the same 401 (not "expired", not "used", not "wrong")."""
        if not ctx.available():
            ctx.events.emit("pairing_exchange_rejected", "unavailable")
            raise AuthError(503, "connector_unavailable")
        result = ctx.pairing.exchange(body.code)
        if result != "ok":
            ctx.events.emit("pairing_exchange_rejected", result)
            if result == "rate_limited":
                raise AuthError(429, "rate_limited", {"Retry-After": str(max(1, ctx.pairing.retry_after()))})
            raise AuthError(401, "pairing_invalid")
        owner = ctx.local_owner()
        token, session, evicted = ctx.sessions.create(owner["id"])
        for ref in evicted:
            ctx.events.emit("session_revoked", "evicted", event_ref=ref)
        written = (ctx.events.emit("pairing_exchange_ok", "ok", principal_id=owner["id"], event_ref=session.ref)
                   and ctx.events.emit("session_created", "ok", principal_id=owner["id"], event_ref=session.ref))
        if not written:
            ctx.sessions.revoke(token)
            raise lost_event()
        return granted(token, session)

    @router.get("/session")
    def session_state(request: Request):
        """Where the session stands. Looking does not count as a use: it never keeps a session from idling out."""
        principal, session, _token = ctx.authenticate(request, touch=False)
        return {"principal": {"id": principal.id, "kind": principal.kind}, **view(session)}

    @router.post("/session/step-up")
    def step_up(body: Code, auth: Annotated[tuple, Depends(ctx.authenticated)]):
        """Confirm again with a fresh code. The confirmation counts min(10 minutes, what is left of the session); the
        token is replaced."""
        principal, session, token = auth
        result = ctx.pairing.exchange(body.code, purpose="step-up", session=session.ref)
        if result != "ok":
            ctx.events.emit("step_up_rejected", result)
            if result == "rate_limited":
                raise AuthError(429, "rate_limited", {"Retry-After": str(max(1, ctx.pairing.retry_after()))})
            raise AuthError(401, "pairing_invalid")
        ctx.sessions.begin_step_up(session)
        new_token = ctx.sessions.rotate(token)
        recorded = new_token and ctx.events.emit("step_up_ok", "ok", principal_id=principal.id, event_ref=session.ref)
        if not recorded:
            ctx.sessions.revoke(new_token or token)
            raise lost_event()
        return {**granted(new_token, session), "step_up_remaining_s": ctx.sessions.remaining(session)["step_up"]}

    @router.post("/session/rotate")
    def rotate(request: Request):
        """A new token for the same session; the old one stops working at once. The end of the session does not move."""
        principal, session, token = ctx.authenticate(request)
        new_token = ctx.sessions.rotate(token)
        if new_token is None or not ctx.events.emit("session_rotated", "ok", principal_id=principal.id,
                                                    event_ref=session.ref):
            ctx.sessions.revoke(new_token or token)
            raise lost_event()
        return granted(new_token, session)

    @router.delete("/session", status_code=204)
    def end_session(request: Request):
        principal, session, token = ctx.authenticate(request, touch=False)
        ctx.sessions.revoke(token)
        ctx.events.emit("session_revoked", "logout", principal_id=principal.id, event_ref=session.ref)
        return Response(status_code=204, headers=SECURITY_HEADERS)

    @router.delete("/sessions", status_code=204)
    def end_all_sessions(request: Request):
        principal, _session, _token = ctx.authenticate(request, touch=False)
        for ref in ctx.sessions.revoke_all(principal.id):
            ctx.events.emit("session_revoked", "revoke_all", principal_id=principal.id, event_ref=ref)
        return Response(status_code=204, headers=SECURITY_HEADERS)

    @router.get("/connector/status")
    def connector_status(request: Request):
        """Which security conditions hold (booleans only). Works while the connector is unavailable, for a session that
        already exists: it is how the reason is found."""
        ctx.authenticate(request, touch=False, require_available=False)
        checks = ctx.checks.snapshot()
        return {"ready": all(checks.values()), "checks": checks}

    return router
