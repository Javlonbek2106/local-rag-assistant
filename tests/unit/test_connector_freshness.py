"""M4.2: is the metadata current, stale, being discovered, failed, or never discovered."""

import datetime

import pytest

from rag.connector import freshness

NOW = datetime.datetime(2026, 11, 20, 12, 0, tzinfo=datetime.timezone.utc)
HOUR = 3600
DAY = 24 * HOUR


def at(seconds_ago):
    return NOW - datetime.timedelta(seconds=seconds_ago)


def compute(**changes):
    facts = {
        "has_snapshot": True,
        "verified_at": at(60),
        "discovery_started_at": None,
        "last_status": "ok",
        "last_error": None,
        "last_attempt_at": at(60),
        "lease_s": 150,
        "max_age_s": 7 * DAY,
    }
    facts.update(changes)
    return freshness.compute(NOW, **facts)


def test_a_confirmed_snapshot_with_nothing_unreviewed_is_current():
    result = compute()
    assert result["state"] == "current" and result["reasons"] == [] and result["snapshot_age_s"] == 60


def test_nothing_ever_discovered():
    result = compute(has_snapshot=False, verified_at=None, last_status=None, last_attempt_at=None)
    assert result["state"] == "never_discovered" and result["snapshot_age_s"] is None


def test_unreviewed_changes_to_what_is_selected_make_it_stale_but_other_changes_do_not():
    assert compute(unreviewed_changes=40)["state"] == "current"  # changes nobody selected anything of
    stale = compute(unreviewed_changes=3, affected_selected=2)
    assert stale["state"] == "stale" and stale["reasons"] == ["unreviewed_changes"] and stale["unreviewed_changes"] == 3


def test_a_selected_object_that_disappeared_makes_it_stale():
    result = compute(removed_selected=1)
    assert result["state"] == "stale" and result["reasons"] == ["removed_selected"]


def test_a_snapshot_not_confirmed_for_longer_than_the_limit_is_stale():
    assert compute(verified_at=at(7 * DAY - 10))["state"] == "current"
    old = compute(verified_at=at(7 * DAY + 10))
    assert old["state"] == "stale" and old["reasons"] == ["age"]


def test_a_failed_last_discovery_is_failed_whatever_else_is_true_and_keeps_the_reason_apart():
    result = compute(last_status="failed", last_error="timeout")
    assert result["state"] == "failed" and result["last_error"] == "timeout" and result["has_snapshot"] is True
    both = compute(last_status="failed", last_error="auth_failed", affected_selected=1)
    assert both["state"] == "failed" and both["reasons"] == ["last_discovery_failed", "unreviewed_changes"]


def test_a_failed_first_discovery_is_failed_not_never_discovered():
    result = compute(has_snapshot=False, verified_at=None, last_status="failed", last_error="unreachable")
    assert result["state"] == "failed" and result["has_snapshot"] is False


def test_a_discovery_running_now_is_in_progress_and_wins_over_everything():
    for extra in (
        {},
        {"last_status": "failed", "last_error": "timeout"},
        {"affected_selected": 5},
        {"has_snapshot": False},
    ):
        assert compute(discovery_started_at=at(10), **extra)["state"] == "in_progress"


def test_a_discovery_that_started_longer_ago_than_the_lease_is_not_running_any_more():
    assert compute(discovery_started_at=at(149))["state"] == "in_progress"
    dead = compute(discovery_started_at=at(151))  # the process that began it is gone
    assert dead["state"] == "current" and dead["in_progress"] is False


def test_the_last_error_is_only_shown_while_the_last_attempt_failed():
    assert compute(last_status="ok", last_error="timeout")["last_error"] is None


@pytest.mark.parametrize("state", ["in_progress", "failed", "never_discovered", "stale", "current"])
def test_the_names_of_the_states_are_the_ones_the_api_promises(state):
    assert state in (freshness.IN_PROGRESS, freshness.FAILED, freshness.NEVER, freshness.STALE, freshness.CURRENT)


def test_the_counts_are_carried_through():
    result = compute(unreviewed_changes=4, affected_selected=0, removed_selected=0, orphaned_descriptions=2)
    assert result["unreviewed_changes"] == 4 and result["orphaned_descriptions"] == 2 and result["state"] == "current"
