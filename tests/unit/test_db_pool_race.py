"""`db.pool()` opens ONE pool even when many threads arrive while it is still unopened (the cold start of a server whose
first requests come together). `functools.lru_cache` alone does not lock: every thread that missed the empty cache
opened a pool of its own and all but one leaked, each with its connections and background threads."""

import threading
import time

from rag import config, db


class SlowPool:
    made = 0
    guard = threading.Lock()

    def __init__(self, *args, **kwargs):
        time.sleep(0.2)  # long enough for every other thread to arrive at the empty cache
        with SlowPool.guard:
            SlowPool.made += 1

    def close(self):
        pass


def test_threads_arriving_together_open_one_pool(monkeypatch):
    monkeypatch.setattr(db, "ConnectionPool", SlowPool)
    monkeypatch.setattr(config, "DB_HARDENED", False)
    SlowPool.made = 0
    db.pool.cache_clear()
    got, errors = [], []

    def call():
        try:
            got.append(db.pool())
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=call) for _ in range(12)]
    try:
        [t.start() for t in threads]
        [t.join(10) for t in threads]
        assert not errors and SlowPool.made == 1 and len({id(p) for p in got}) == 1 and len(got) == 12
    finally:
        db.pool.cache_clear()


def test_the_cache_can_still_be_inspected_and_cleared_as_before(monkeypatch):
    monkeypatch.setattr(db, "ConnectionPool", SlowPool)
    monkeypatch.setattr(config, "DB_HARDENED", False)
    SlowPool.made = 0
    db.pool.cache_clear()
    assert db.pool.cache_info().currsize == 0
    first = db.pool()
    assert db.pool() is first and db.pool.cache_info().currsize == 1
    db.close()
    assert db.pool.cache_info().currsize == 0
