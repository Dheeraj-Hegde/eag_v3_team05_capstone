"""Per-run JSONL trace recorder. Every event lands on disk before scoring."""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def new_run_id(task_id: str | None = None) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    short = uuid.uuid4().hex[:6]
    return f"{task_id}-{stamp}-{short}" if task_id else f"{stamp}-{short}"


class Trace:
    def __init__(self, root: str | os.PathLike[str], run_id: str, *, task_id: str | None = None) -> None:
        self.run_id = run_id
        self.task_id = task_id
        self.dir = Path(root) / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self._events_path = self.dir / "trace.jsonl"
        self._meta_path = self.dir / "meta.json"
        self._fh = self._events_path.open("a", encoding="utf-8")
        self._started = time.time()
        self._meta: dict[str, Any] = {
            "run_id": run_id,
            "task_id": task_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write_meta()

    def event(self, kind: str, payload: dict[str, Any] | None = None) -> None:
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "elapsed_ms": int((time.time() - self._started) * 1000),
            "kind": kind,
        }
        if payload:
            rec["payload"] = payload
        self._fh.write(json.dumps(rec, default=str) + "\n")
        self._fh.flush()

    def set_meta(self, **kwargs: Any) -> None:
        self._meta.update(kwargs)
        self._write_meta()

    def _write_meta(self) -> None:
        self._meta_path.write_text(json.dumps(self._meta, indent=2, default=str), encoding="utf-8")

    def close(self, *, final_answer: str | None = None, refused: bool | None = None) -> None:
        self._meta["ended_at"] = datetime.now(timezone.utc).isoformat()
        self._meta["duration_ms"] = int((time.time() - self._started) * 1000)
        if final_answer is not None:
            self._meta["final_answer"] = final_answer
        if refused is not None:
            self._meta["refused"] = refused
        self._write_meta()
        self._fh.close()

    def __enter__(self) -> "Trace":
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()
