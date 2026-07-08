"""The "too new to judge" guard — pure working-out over the dead set and first-seen dates.

A node created recently has had little chance to be queried, so calling it "unused" would be
false-confident. Its first appearance in the job history stands in for its creation date (an
old model rebuilt nightly has jobs throughout the window; a new one first appears when it was
created). Nodes never seen at all are judged normally — no jobs in the whole window is the
strongest "unused" signal there is.
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
