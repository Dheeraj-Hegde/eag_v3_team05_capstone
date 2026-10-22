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
    # "either" = refusing or producing are both acceptable; the verifier decides.
    expected: Literal["produce", "refuse", "either"]
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
    title: str = ""
    score: float = 0.0
    stopped_reason: str = ""
    tokens_used: int = 0
    verifier_evidence: dict[str, Any] = Field(default_factory=dict)
    final_text_excerpt: str = ""
