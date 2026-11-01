"""Shared location of the durable night-run plan mirror."""
from pathlib import Path


def night_plan_mirror(state_dir: Path, run_id: str) -> Path:
    return Path(state_dir) / "night-runs" / run_id / "plan.md"
