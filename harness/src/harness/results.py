"""Official AgentSwitch `results.json` format (Release 8.1).

{"tasks": [{"id", "title", "passed": bool, "score": 0..1, "evidence": str}],
 "summary": "one line"}
1–200 tasks. A missing or malformed file shows "no results" on the panel, so
we validate before writing and the CLI can re-validate before submitting.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

MAX_TASKS = 200
MAX_EVIDENCE_CHARS = 300


def _one_line(s: str, limit: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def official_task(
    task_id: str,
    title: str,
    passed: bool,
    evidence: str,
    score: float | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": str(task_id),
        "title": _one_line(title or task_id, 120),
        "passed": bool(passed),
        "evidence": _one_line(evidence, MAX_EVIDENCE_CHARS),
    }
    entry["score"] = float(min(1.0, max(0.0, score if score is not None else (1.0 if passed else 0.0))))
    return entry


def build_document(tasks: list[dict[str, Any]], *, instance: str | None, model: str | None, note: str = "") -> dict[str, Any]:
    passed = sum(1 for t in tasks if t["passed"])
    parts = [f"Seat 05 Stock: {passed}/{len(tasks)} passed", f"instance={instance or '?'}", f"model={model or '?'}"]
    if note:
        parts.append(note)
    return {"tasks": tasks, "summary": _one_line(" | ".join(parts), 240)}


def validate_document(doc: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["top level must be an object"]
    tasks = doc.get("tasks")
    if not isinstance(tasks, list):
        errors.append("'tasks' must be a list")
        tasks = []
    elif not (1 <= len(tasks) <= MAX_TASKS):
        errors.append(f"'tasks' must have 1–{MAX_TASKS} entries, got {len(tasks)}")
    if not isinstance(doc.get("summary"), str):
        errors.append("'summary' must be a string")
    elif "\n" in doc["summary"]:
        errors.append("'summary' must be one line")
    seen: set[str] = set()
    for i, t in enumerate(tasks):
        where = f"tasks[{i}]"
        if not isinstance(t, dict):
            errors.append(f"{where} must be an object")
            continue
        if not isinstance(t.get("id"), str) or not t["id"]:
            errors.append(f"{where}.id must be a non-empty string")
        elif t["id"] in seen:
            errors.append(f"{where}.id '{t['id']}' is duplicated")
        else:
            seen.add(t["id"])
        if not isinstance(t.get("title"), str):
            errors.append(f"{where}.title must be a string")
        if not isinstance(t.get("passed"), bool):
            errors.append(f"{where}.passed must be true or false")
        if "score" in t:
            s = t["score"]
            if isinstance(s, bool) or not isinstance(s, (int, float)) or not (0 <= s <= 1):
                errors.append(f"{where}.score must be a number between 0 and 1")
        if "evidence" in t and not isinstance(t["evidence"], str):
            errors.append(f"{where}.evidence must be a string")
    return errors


def write_document(doc: dict[str, Any], path: str | Path) -> None:
    errors = validate_document(doc)
    if errors:
        raise ValueError("results.json would be invalid: " + "; ".join(errors))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replace so a timeout mid-write never leaves a half file.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".results-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def validate_file(path: str | Path) -> list[str]:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [f"{path} not found"]
    except json.JSONDecodeError as e:
        return [f"{path} is not valid JSON: {e}"]
    return validate_document(doc)
