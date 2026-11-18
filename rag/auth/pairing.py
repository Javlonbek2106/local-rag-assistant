"""The one-time pairing code (decision D-3): how a person at this computer proves that they are the one who started
the application, without any password.

The code is made on request, shown ONLY in the console of the running application (never in a URL, a log, a file or a
response), lives briefly, and is used up by the first exchange that is made against it, whatever the outcome. It is not
a session token: it can only be exchanged, once, for a session.

Rules of this board (numbers come from rag.config and are implementation parameters, not decisions):
- one code at a time; a new request replaces the old one and cannot come sooner than PAIRING_REQUEST_GAP_S;
- every exchange attempt, right or wrong, uses up the code that is waiting (so a guess is one try per code, and a
  wrong guess costs its author nothing but the code, never reveals anything);
- at most PAIRING_ATTEMPTS attempts per PAIRING_WINDOW_S; the next one starts a lockout of PAIRING_LOCKOUT_S, during
  which nothing is evaluated and the waiting code is destroyed;
- only a digest of the code is kept, and compared in constant time.
"""
import hashlib
import hmac
import secrets
import sys
import threading
import time

from rag import config


def _digest(code):
    return hashlib.sha256(code.encode("ascii")).digest()


def to_console(text):
    """The only way a code leaves this module: the console of the running application (not the logging system)."""
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


class PairingBoard:
    def __init__(self, clock=time.monotonic, show=to_console):
        self._clock, self._show = clock, show
        self._lock = threading.Lock()
        self._pending = None  # {"digest", "expires", "purpose", "session"}
        self._last_request = None
        self._attempts = []  # times of the recent exchange attempts
        self._locked_until = 0.0

    def request(self, purpose="login", session=None):
        """Make a new code and show it in the console. "ok"; "rate_limited" (nothing is made or shown); or "unavailable"
        when the console cannot be written to (no code is kept: one nobody can see would only be a target)."""
        with self._lock:
            now = self._clock()
            if self._last_request is not None and now - self._last_request < config.PAIRING_REQUEST_GAP_S:
                return "rate_limited"
            if now < self._locked_until:
                return "rate_limited"
            code = "".join(secrets.choice("0123456789") for _ in range(config.PAIRING_DIGITS))
            half = config.PAIRING_DIGITS // 2
            try:
                self._show(f"[juftlash] kod: {code[:half]} {code[half:]}  ({config.PAIRING_TTL_S} soniya amal qiladi, "
                           f"bir marta ishlatiladi)")
            except Exception:  # noqa: BLE001 - no console (closed, detached): nobody can read a code, so none is made
                self._pending = None
                return "unavailable"
            self._last_request = now
            self._pending = {"digest": _digest(code), "expires": now + config.PAIRING_TTL_S, "purpose": purpose,
                             "session": session}
            return "ok"

    def exchange(self, code, purpose="login", session=None):
        """"ok" if `code` is the waiting code for this purpose (and session); "invalid" otherwise (wrong, expired, used,
        none waiting: all the same word); "rate_limited" during a lockout. The waiting code is used up either way."""
        with self._lock:
            now = self._clock()
            if now < self._locked_until:
                self._pending = None
                return "rate_limited"
            self._attempts = [t for t in self._attempts if now - t < config.PAIRING_WINDOW_S]
            if len(self._attempts) >= config.PAIRING_ATTEMPTS:
                self._locked_until = now + config.PAIRING_LOCKOUT_S
                self._attempts = []
                self._pending = None
                return "rate_limited"
            self._attempts.append(now)
            pending, self._pending = self._pending, None  # used up by this attempt, whatever happens next
            typed = "".join(ch for ch in str(code) if ch not in " -")
            well_formed = typed.isascii() and typed.isdigit() and len(typed) == config.PAIRING_DIGITS
            if pending is None or now >= pending["expires"] or not well_formed:
                return "invalid"
            if pending["purpose"] != purpose or pending["session"] != session:
                return "invalid"
            return "ok" if hmac.compare_digest(_digest(typed), pending["digest"]) else "invalid"

    def retry_after(self):
        """Seconds until a lockout ends (0 when there is none)."""
        with self._lock:
            return max(0, int(self._locked_until - self._clock()) + 1) if self._clock() < self._locked_until else 0
