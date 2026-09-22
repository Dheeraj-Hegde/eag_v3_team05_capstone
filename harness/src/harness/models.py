"""Pydantic models for tasks and results."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Verifier(BaseModel):
    fn: str
    spec: dict[str, Any] = Field(default_factory=dict)


class Task(BaseModel):
    id: str
    title: str
    prompt: str
    expected: Literal["produce", "refuse"]
    verifier: Verifier
    preconditions: str | None = None
    notes: str | None = None


class TaskResult(BaseModel):
    task_id: str
    run_id: str
    passed: bool
    reason: str
    expected: str
    refused: bool
    trace_dir: str
    verifier_evidence: dict[str, Any] = Field(default_factory=dict)
    final_text_excerpt: str = ""
