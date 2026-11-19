"""Who is asking, and may they (the S1 boundary of ADR-P16): `current_principal`, `authorize` and `require_step_up`.

An AuthContext holds the pieces (pairing board, sessions, security log, checks) for one application. The connector's
routes ask it who the caller is; the existing document and ask routes never do (decision D-1).

Failures are AuthErrors: a status and a fixed code, answered as {"error": {"code": ...}} and never with a message that
tells an attacker more than "no". Expired, unknown and malformed sessions are all the same 401.
"""
import logging
import threading
import time
from typing import NamedTuple

from fastapi import Request
from fastapi.responses import JSONResponse

from rag import config
from rag.auth import secretfiles
from rag.auth.checks import SecurityChecks, default_checks
from rag.auth.events import SecurityLog
from rag.auth.pairing import PairingBoard
from rag.auth.principals import PrincipalError, Principals
from rag.auth.sessions import SessionStore

log = logging.getLogger(__name__)
SECURITY_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
MAX_TOKEN_LENGTH = 128  # a real token is 43 characters; anything much longer is not one and is not even hashed
REJECT_EVENT_GAP_S = 10  # a flood of bad requests writes one "session_rejected" event per reason per this long


class AuthError(Exception):
    def __init__(self, status, code, headers=None):
        super().__init__(code)
        self.status, self.code, self.headers = status, code, dict(headers or {})


def auth_error_response(_request, error):
    return JSONResponse({"error": {"code": error.code}}, status_code=error.status,
                        headers={**SECURITY_HEADERS, **error.headers})


class Principal(NamedTuple):
    id: str
    kind: str


class AuthContext:
    def __init__(self, principals, events, checks=None, sessions=None, pairing=None, clock=time.monotonic, dirs=None):
        self.principals, self.events, self.clock = principals, events, clock
        self.checks = checks or SecurityChecks(default_checks(principals, events), events, clock)
        self.sessions = sessions or SessionStore(clock)
        self.pairing = pairing or PairingBoard(clock)
        self._dirs = dirs if dirs is not None else (lambda: [config.SECRETS_DIR, config.SECURITY_LOG_DIR])
        self._lock = threading.Lock()
        self._owner, self._owner_at = None, 0.0
        self._last_reject = {}

    # ----- start-up and readiness -----

    def startup(self):
        """Make the private directories (or find them unacceptable), run every check, and record the outcome. Never
        raises: whatever goes wrong leaves the connector unavailable, not the application down."""
        for directory in self._dirs() if callable(self._dirs) else self._dirs:
            try:
                secretfiles.ensure_private_dir(directory)
            except (OSError, secretfiles.SecretError):
                log.warning("connector: a private directory is not available")
        result = self.checks.snapshot(force=True)
        failed = [name for name, ok in result.items() if not ok]
        self.events.emit("startup_security_check", "check_failed" if failed else "ok", failed=failed)
        if failed:
            log.warning("connector unavailable, failed security checks: %s", ", ".join(sorted(failed)))
        return result

    def available(self):
        return self.checks.ok()

    def readiness_word(self):
        return "ok" if self.available() else "unavailable"

    # ----- who is asking -----

    def local_owner(self):
        """The local owner, kept for SECURITY_CHECK_TTL_S so that a request does not query the database for it."""
        with self._lock:
            now = self.clock()
            if self._owner is None or now - self._owner_at >= config.SECURITY_CHECK_TTL_S:
                try:
                    self._owner, self._owner_at = self.principals.local_owner(), now
                except PrincipalError:
                    self._owner = None
                    raise AuthError(503, "connector_unavailable") from None
            return self._owner

    def _rejected(self, reason):
        now = self.clock()
        if now - self._last_reject.get(reason, -REJECT_EVENT_GAP_S) >= REJECT_EVENT_GAP_S:
            self._last_reject[reason] = now
            self.events.emit("session_rejected", reason)

    def bearer(self, request):
        """The token of an `Authorization: Bearer <token>` header; AuthError when there is none (nothing else counts:
        not a query parameter, not a cookie, not a body)."""
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if not header:
            self._rejected("missing")
            raise AuthError(401, "unauthenticated", {"WWW-Authenticate": "Bearer"})
        if scheme.lower() != "bearer" or not token.strip() or len(token.strip()) > MAX_TOKEN_LENGTH:
            self._rejected("malformed")
            raise AuthError(401, "unauthenticated", {"WWW-Authenticate": "Bearer"})
        return token.strip()

    def authenticate(self, request, touch=True, require_available=True):
        """(principal, session, token) of a valid session, or an AuthError. `touch=False` looks without counting as
        a use (the idle time is not restarted)."""
        if require_available and not self.available():
            raise AuthError(503, "connector_unavailable")
        token = self.bearer(request)
        found = self.sessions.lookup(token, touch=touch)
        if found.session is None:
            if found.reason in ("idle", "absolute"):
                self.events.emit("session_revoked", found.reason, event_ref=found.ref)
                self._rejected(found.reason)
                raise AuthError(401, "session_expired", {"WWW-Authenticate": "Bearer"})
            self._rejected("missing")
            raise AuthError(401, "unauthenticated", {"WWW-Authenticate": "Bearer"})
        owner = self.local_owner()
        if found.session.principal_id != owner["id"]:
            self._rejected("denied")
            raise AuthError(401, "unauthenticated", {"WWW-Authenticate": "Bearer"})
        principal = Principal(owner["id"], owner["kind"])
        self.authorize(principal, "session")  # the one decision point is really in the path, not only available
        return principal, found.session, token

    def authorize(self, principal, action, resource=None):
        """The one place that decides whether a principal may do something. In S1 there is one principal and it may
        do everything the connector offers; any other kind is refused. Later phases add real rules here only."""
        if principal.kind != "local-owner":
            self._rejected("denied")
            raise AuthError(403, "forbidden")

    # ----- dependencies for routes -----

    def authenticated(self, request: Request):
        """(principal, session, token) as a route dependency: resolved before the body of the request is read, so a
        caller without a session gets the 401 whatever the body is."""
        return self.authenticate(request)

    def current_principal(self, request: Request):
        principal, session, _token = self.authenticate(request)
        request.state.session = session
        return principal

    def require_step_up(self, request: Request):
        """For an operation that needs a fresh confirmation (a later milestone applies it): a principal whose
        session has a step-up that is still running."""
        principal, session, _token = self.authenticate(request)
        if self.sessions.remaining(session)["step_up"] <= 0:
            raise AuthError(403, "step_up_required")
        request.state.session = session
        return principal


def build_context():
    """The real thing: the application's own database, the user's profile directories, the real clock."""
    events = SecurityLog()
    return AuthContext(Principals(), events)
