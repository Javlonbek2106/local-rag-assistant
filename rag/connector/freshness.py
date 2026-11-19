"""Is what the connector knows about an external schema current? (roadmap M4.2). Pure: the caller gathers the facts.

States, in this order of precedence:
- `in_progress`      a discovery is running (it started less than `lease_s` ago: a crashed one stops counting);
- `failed`           the last discovery did not finish; the previous snapshot, if there is one, is kept and still
  readable;
- `never_discovered` there is no snapshot and no attempt;
- `stale`            a snapshot exists and the last attempt worked, but it cannot be trusted as it stands: the schema
  changed in a way that touches what is selected and nobody has reviewed it, or a selected object has disappeared, or
  the snapshot was last confirmed longer ago than `max_age_s`;
- `current`          none of the above.
`reasons` lists every cause that applies (a failed discovery can also have unreviewed changes). Nothing here decides
what a caller may do with a state: context.py says "not usable" for everything but `current`."""

IN_PROGRESS, FAILED, NEVER, STALE, CURRENT = "in_progress", "failed", "never_discovered", "stale", "current"


def seconds_between(later, earlier):
    return (later - earlier).total_seconds()


def compute(
    now,
    *,
    has_snapshot,
    verified_at,
    discovery_started_at,
    last_status,
    last_error,
    last_attempt_at,
    lease_s,
    max_age_s,
    unreviewed_changes=0,
    affected_selected=0,
    removed_selected=0,
    orphaned_descriptions=0,
):
    running = discovery_started_at is not None and seconds_between(now, discovery_started_at) < lease_s
    age = seconds_between(now, verified_at) if has_snapshot and verified_at is not None else None
    reasons = []
    if last_status == "failed":
        reasons.append("last_discovery_failed")
    if has_snapshot:
        if affected_selected:
            reasons.append("unreviewed_changes")
        if removed_selected:
            reasons.append("removed_selected")
        if age is not None and age > max_age_s:
            reasons.append("age")
    if running:
        state = IN_PROGRESS
    elif last_status == "failed":
        state = FAILED
    elif not has_snapshot:
        state = NEVER
    elif reasons:
        state = STALE
    else:
        state = CURRENT
    return {
        "state": state,
        "reasons": reasons,
        "has_snapshot": bool(has_snapshot),
        "in_progress": bool(running),
        "last_attempt_at": last_attempt_at,
        "last_error": last_error if last_status == "failed" else None,
        "snapshot_age_s": None if age is None else int(age),
        "unreviewed_changes": int(unreviewed_changes),
        "affected_selected": int(affected_selected),
        "removed_selected": int(removed_selected),
        "orphaned_descriptions": int(orphaned_descriptions),
    }
