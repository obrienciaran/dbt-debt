"""The "too new to judge" guard — pure working-out over the dead set and first-seen dates.

A node created recently has had little chance to be queried, so calling it "unused" would be
false-confident. Its first appearance in the job history stands in for its creation date (an
old model rebuilt nightly has jobs throughout the window; a new one first appears when it was
created). What a *missing* first-seen date means is the caller's call, because it differs by
warehouse: on BigQuery it means zero jobs in the whole lookback window — the strongest "unused"
signal there is — so those nodes are judged normally; on Snowflake first-seen comes from
`ACCOUNT_USAGE.TABLES.created`, which lags (documented ~90 minutes), so a missing row means the
metadata has not caught up yet — likely a new table — and `missing_first_seen_models` sets those
aside instead.
"""

from __future__ import annotations

from collections.abc import Mapping, Set
from datetime import datetime, timedelta


def too_new_models(
    dead: Set[str],
    first_seen: Mapping[str, datetime],
    now: datetime,
    min_age: timedelta,
) -> set[str]:
    """The dead nodes whose first job is younger than `min_age` — too new to call unused.

    `now` is a parameter so the verdict stays pure and testable; a zero (or negative)
    `min_age` disables the guard.
    """

    if min_age <= timedelta(0):
        return set()
    threshold = now - min_age
    return {uid for uid in dead if uid in first_seen and first_seen[uid] > threshold}


def missing_first_seen_models(dead: Set[str], first_seen: Mapping[str, datetime]) -> set[str]:
    """The dead nodes with no first-seen date at all — age cannot be proven.

    Pure set arithmetic; whether a missing date means "brand new" (Snowflake's lagging
    `ACCOUNT_USAGE.TABLES`) or "no jobs all window" (BigQuery) is the caller's decision.
    """

    return {uid for uid in dead if uid not in first_seen}
