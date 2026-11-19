"""Small stand-ins shared by the authentication tests: a clock a test can move, a fake principal store, an event
sink."""
OWNER_ID = "6f9619ff-8b86-d011-b42d-00c04fc964ff"


class Clock:
    """A monotonic clock that only moves when the test says so."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakePrincipals:
    def __init__(self, owner_id=OWNER_ID, failure=None):
        self.owner_id, self.failure = owner_id, failure

    def local_owner(self):
        if self.failure:
            raise self.failure
        return {"id": self.owner_id, "kind": "local-owner"}


class Events:
    """Collects what would go into the security log; `lost` makes every write fail, as a full disk would."""

    def __init__(self):
        self.items, self.lost, self.healthy = [], False, True

    def emit(self, event, reason, principal_id=None, event_ref=None, failed=()):
        if self.lost:
            self.healthy = False
            return False
        self.healthy = True
        self.items.append({"event": event, "reason": reason, "principal_id": principal_id, "event_ref": event_ref,
                           "failed": list(failed)})
        return True

    def check(self):
        return self.healthy

    def names(self):
        return [(i["event"], i["reason"]) for i in self.items]
