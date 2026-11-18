"""Sessions of the authenticated state S1 (decision D-4): in the memory of this process, nowhere else.

- A session lives at most SESSION_ABSOLUTE_S from its creation, and ends earlier after SESSION_IDLE_S without a request
  that uses it. Both limits are checked here, on the server, with a monotonic clock (the wall clock can be changed, the
  browser's clock is never asked).
- A step-up confirmation counts for STEP_UP_S, but never past the end of the session: min(STEP_UP_S, what is left).
- Rotation gives the session a new token and ends the old one at once; it does not extend the absolute lifetime.
- Nothing is stored on disk and nothing survives a restart: when the process ends, every session ends.
- The token is 256 random bits; only its SHA-256 is kept (as the key), so a dump of this memory holds no usable token.
  `ref` identifies a session in the security events; it is random and not derived from the token.
"""
import hashlib
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from typing import NamedTuple

from rag import config


@dataclass
class Session:
    ref: str
    principal_id: str
    created: float
    last_seen: float
    step_up_until: float | None = None


class Lookup(NamedTuple):
    session: Session | None
    reason: str | None  # None when the session is valid; else "missing", "absolute" or "idle"
    ref: str | None  # the reference of a session that has just expired (for the event), else None


def _key(token):
    return hashlib.sha256(str(token).encode("utf-8", "replace")).hexdigest()


class SessionStore:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions = {}  # key of the token -> Session

    def _expired(self, session, now):
        if now - session.created >= config.SESSION_ABSOLUTE_S:
            return "absolute"
        if now - session.last_seen >= config.SESSION_IDLE_S:
            return "idle"
        return None

    def create(self, principal_id):
        """A new session: (token, session, references of the sessions that had to make room for it)."""
        with self._lock:
            now = self._clock()
            for key in [k for k, s in self._sessions.items() if self._expired(s, now)]:
                del self._sessions[key]
            evicted = []
            while len(self._sessions) >= config.SESSION_MAX_COUNT:
                oldest = min(self._sessions, key=lambda k: self._sessions[k].created)
                evicted.append(self._sessions.pop(oldest).ref)
            token = secrets.token_urlsafe(32)
            session = Session(ref=str(uuid.uuid4()), principal_id=principal_id, created=now, last_seen=now)
            self._sessions[_key(token)] = session
            return token, session, evicted

    def lookup(self, token, touch=True):
        """The session of `token` if it is valid now (with `touch`, this counts as a use: it restarts the idle time)."""
        with self._lock:
            now = self._clock()
            key = _key(token)
            session = self._sessions.get(key)
            if session is None:
                return Lookup(None, "missing", None)
            reason = self._expired(session, now)
            if reason:
                del self._sessions[key]
                return Lookup(None, reason, session.ref)
            if touch:
                session.last_seen = now
            return Lookup(session, None, None)

    def rotate(self, token):
        """A new token for the same session (same reference, same end of life); the old token stops working at once."""
        with self._lock:
            now = self._clock()
            session = self._sessions.get(_key(token))
            if session is None or self._expired(session, now):
                return None
            del self._sessions[_key(token)]
            session.last_seen = now
            new_token = secrets.token_urlsafe(32)
            self._sessions[_key(new_token)] = session
            return new_token

    def revoke(self, token):
        with self._lock:
            return self._sessions.pop(_key(token), None)

    def revoke_all(self, principal_id):
        """End every session of the principal; the references of the ended sessions."""
        with self._lock:
            keys = [k for k, s in self._sessions.items() if s.principal_id == principal_id]
            return [self._sessions.pop(k).ref for k in keys]

    def begin_step_up(self, session):
        with self._lock:
            now = self._clock()
            session.step_up_until = min(now + config.STEP_UP_S, session.created + config.SESSION_ABSOLUTE_S)

    def remaining(self, session):
        """Seconds left, as the page shows them: {"absolute", "idle", "step_up"} (never negative)."""
        now = self._clock()
        absolute = session.created + config.SESSION_ABSOLUTE_S - now
        idle = session.last_seen + config.SESSION_IDLE_S - now
        step_up = (session.step_up_until - now) if session.step_up_until is not None else 0
        return {"absolute": max(0, int(absolute)), "idle": max(0, int(min(idle, absolute))),
                "step_up": max(0, int(min(step_up, absolute)))}

    def clear(self):
        with self._lock:
            self._sessions.clear()

    def count(self):
        with self._lock:
            return len(self._sessions)
