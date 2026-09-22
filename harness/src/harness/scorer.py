"""Score walker: reads runs/ and prints the pass/fail table."""

from __future__ import annotations

import json
from pathlib import Path

from harness.models import TaskResult


def collect_latest_results(runs_root: str | Path = "runs") -> list[TaskResult]:
    root = Path(runs_root)
    if not root.exists():
        return []
    out: list[TaskResult] = []
    for task_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        runs = sorted((p for p in task_dir.iterdir() if p.is_dir()), key=lambda p: p.name)
        if not runs:
            continue
        latest = runs[-1] / "result.json"
        if not latest.exists():
            continue
        out.append(TaskResult.model_validate_json(latest.read_text(encoding="utf-8")))
    return out


def render_table(results: list[TaskResult]) -> str:
    if not results:
        return "no results found in runs/"
    header = f"{'task':<8} {'expected':<9} {'refused':<8} {'passed':<7} reason"
    lines = [header, "-" * max(len(header), 60)]
    for r in results:
        lines.append(
            f"{r.task_id:<8} {r.expected:<9} {str(r.refused):<8} {str(r.passed):<7} {r.reason}"
        )
    fails = sum(1 for r in results if not r.passed)
    lines.append("")
    lines.append(f"{len(results) - fails} passed, {fails} failed, {len(results)} total")
    return "\n".join(lines)


def exit_code(results: list[TaskResult]) -> int:
    return 0 if all(r.passed for r in results) else 1
