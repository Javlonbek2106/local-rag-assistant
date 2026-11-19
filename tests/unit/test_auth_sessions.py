"""Roadmap M4.1a, decision D-4: sessions of the authenticated state. 12 minutes at most, 2 idle, 10 for a step-up that
can never outlive the session; in memory only."""
import pytest

from rag import config
from rag.auth.sessions import SessionStore
from tests.auth_support import OWNER_ID, Clock

MINUTE = 60


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(clock):
    return SessionStore(clock=clock)


def test_the_accepted_values_are_twelve_two_and_ten_minutes():
    values = (config.SESSION_ABSOLUTE_S, config.SESSION_IDLE_S, config.STEP_UP_S)
    assert values == (12 * MINUTE, 2 * MINUTE, 10 * MINUTE)


def test_a_new_session_is_valid_and_its_token_is_random_and_long(store):
    token, session, evicted = store.create(OWNER_ID)
    assert len(token) >= 43 and evicted == []  # 32 random bytes
    found = store.lookup(token)
    assert found.session is session and found.reason is None
    assert store.create(OWNER_ID)[0] != token


def test_an_unknown_token_is_missing(store):
    assert store.lookup("nothing").reason == "missing" and store.lookup("").reason == "missing"


def test_the_token_itself_is_never_kept(store):
    token, _, _ = store.create(OWNER_ID)
    assert token not in repr(vars(store)) and token not in str(store._sessions)


def test_a_session_idles_out_after_two_minutes_without_use(store, clock):
    token, session, _ = store.create(OWNER_ID)
    clock.advance(2 * MINUTE - 1)
    assert store.lookup(token).reason is None  # this use restarts the idle time
    clock.advance(2 * MINUTE - 1)
    assert store.lookup(token).reason is None
    clock.advance(2 * MINUTE)
    found = store.lookup(token)
    assert found.session is None and found.reason == "idle" and found.ref == session.ref
    assert store.lookup(token).reason == "missing"  # and it is gone for good


def test_a_session_ends_after_twelve_minutes_however_busy_it_is(store, clock):
    token, session, _ = store.create(OWNER_ID)
    for _ in range(11):
        clock.advance(MINUTE)
        assert store.lookup(token).reason is None  # used every minute: never idle
    clock.advance(MINUTE)
    found = store.lookup(token)
    assert found.reason == "absolute" and found.ref == session.ref


def test_looking_without_touching_does_not_keep_a_session_alive(store, clock):
    token, _, _ = store.create(OWNER_ID)
    clock.advance(MINUTE + 30)
    assert store.lookup(token, touch=False).reason is None
    clock.advance(MINUTE)
    assert store.lookup(token).reason == "idle"  # the look at 1:30 did not count as use


def test_a_step_up_lasts_ten_minutes_when_the_session_has_that_long_left(store, clock):
    token, session, _ = store.create(OWNER_ID)
    store.begin_step_up(session)
    assert store.remaining(session)["step_up"] == 10 * MINUTE - 0
    clock.advance(MINUTE)
    store.lookup(token)
    assert store.remaining(session)["step_up"] == 9 * MINUTE


def keep_busy(store, clock, token, minutes):
    """Use the session every minute for `minutes` minutes, so that it never idles out."""
    for _ in range(minutes):
        clock.advance(MINUTE)
        assert store.lookup(token).reason is None


def test_a_step_up_can_never_outlive_the_session(store, clock):
    token, session, _ = store.create(OWNER_ID)
    keep_busy(store, clock, token, 5)
    store.begin_step_up(session)  # 7 minutes of the session are left, so the step-up counts 7, not 10
    left = store.remaining(session)
    assert left["absolute"] == 7 * MINUTE and left["step_up"] == 7 * MINUTE
    clock.advance(7 * MINUTE)
    assert store.lookup(token).reason == "absolute"


def test_no_step_up_means_no_step_up_time(store):
    _, session, _ = store.create(OWNER_ID)
    assert store.remaining(session)["step_up"] == 0


def test_a_step_up_expires_while_the_session_goes_on(store, clock):
    token, session, _ = store.create(OWNER_ID)
    store.begin_step_up(session)
    for _ in range(10):  # busy session: never idle
        clock.advance(MINUTE)
        assert store.lookup(token).reason is None
    assert store.remaining(session)["step_up"] == 0 and store.remaining(session)["absolute"] == 2 * MINUTE


def test_rotation_gives_a_new_token_ends_the_old_one_and_keeps_the_end_of_life(store, clock):
    token, session, _ = store.create(OWNER_ID)
    keep_busy(store, clock, token, 5)
    new = store.rotate(token)
    assert new and new != token and store.lookup(token).reason == "missing"
    found = store.lookup(new)
    assert found.session is session and found.session.ref == session.ref
    assert store.remaining(session)["absolute"] == 7 * MINUTE  # rotating did not extend it


def test_rotation_of_a_dead_or_unknown_session_does_nothing(store, clock):
    token, _, _ = store.create(OWNER_ID)
    assert store.rotate("unknown") is None
    clock.advance(12 * MINUTE)
    assert store.rotate(token) is None


def test_a_step_up_survives_rotation(store):
    token, session, _ = store.create(OWNER_ID)
    store.begin_step_up(session)
    new = store.rotate(token)
    assert store.remaining(store.lookup(new).session)["step_up"] > 0


def test_revoking_one_session_leaves_the_others(store):
    a, sa, _ = store.create(OWNER_ID)
    b, _, _ = store.create(OWNER_ID)
    assert store.revoke(a) is sa
    assert store.lookup(a).reason == "missing" and store.lookup(b).reason is None
    assert store.revoke(a) is None


def test_revoking_all_ends_every_session_of_the_principal(store):
    tokens = [store.create(OWNER_ID)[0] for _ in range(3)]
    other = store.create("another-principal")[0]
    assert len(store.revoke_all(OWNER_ID)) == 3
    assert all(store.lookup(t).reason == "missing" for t in tokens) and store.lookup(other).reason is None


def test_a_restart_ends_every_session(clock):
    """Sessions exist only in the memory of the store: a new store, as after a restart, knows none of them."""
    first = SessionStore(clock=clock)
    token, _, _ = first.create(OWNER_ID)
    assert SessionStore(clock=clock).lookup(token).reason == "missing"


def test_only_so_many_sessions_live_at_once_and_the_oldest_makes_room(store, clock):
    tokens = []
    for _ in range(config.SESSION_MAX_COUNT):
        tokens.append(store.create(OWNER_ID))
        clock.advance(1)
    token, _, evicted = store.create(OWNER_ID)
    assert evicted == [tokens[0][1].ref] and store.count() == config.SESSION_MAX_COUNT
    assert store.lookup(tokens[0][0]).reason == "missing" and store.lookup(token).reason is None


def test_expired_sessions_are_dropped_when_a_new_one_is_made(store, clock):
    for _ in range(3):
        store.create(OWNER_ID)
    clock.advance(12 * MINUTE)
    store.create(OWNER_ID)
    assert store.count() == 1


def test_remaining_time_is_never_negative_and_idle_never_exceeds_the_end(store, clock):
    token, session, _ = store.create(OWNER_ID)
    clock.advance(11 * MINUTE + 30)
    store.lookup(token)
    left = store.remaining(session)
    assert left["idle"] <= left["absolute"] == 30
