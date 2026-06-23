"""Render a `Scorecard` as machine-readable JSON for CI consumption."""

from __future__ import annotations

import json
from dataclasses import asdict

from dbt_debt.report.scorecard import OrphanReport, Scorecard


def render_json(scorecard: Scorecard) -> str:
    """Serialize the scorecard to indented JSON."""

    return json.dumps(asdict(scorecard), indent=2)


def render_orphans_json(scorecard: Scorecard) -> str:
    """Serialize just the orphan section to indented JSON for the focused `--orphans` report."""

    orphans = scorecard.orphans if scorecard.orphans is not None else OrphanReport()
    payload = {"project_name": scorecard.project_name, "orphans": asdict(orphans)}
    return json.dumps(payload, indent=2)
