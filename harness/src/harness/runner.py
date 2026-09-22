"""Task runner. Loads a task, invokes the agent, writes result.json before scoring."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from stock_agent.agent import Agent, AgentConfig
from stock_agent.mcp_client import MCPClient

from harness.models import Task, TaskResult
from harness.verifiers import dispatch


def load_task(path: str | Path) -> Task:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Task.model_validate(data)


def run_task(task: Task, *, runs_root: str | Path = "runs") -> TaskResult:
    cfg = AgentConfig.from_env()
    agent = Agent(cfg)
    agent_result = agent.run(task.prompt, task_id=task.id)

    task_dir = Path(runs_root) / task.id / agent_result.run_id
    task_dir.mkdir(parents=True, exist_ok=True)

    with MCPClient(cfg.as_base_url, cfg.as_token) as mcp:
        mcp.initialize()
        passed, reason, evidence = dispatch(
            task.verifier.fn, task.verifier.spec, mcp, agent_result
        )

    expected_match = _expected_matches(task.expected, passed, agent_result.refused)
    final_passed = passed and expected_match
    if not expected_match:
        reason = f"expected={task.expected} but refused={agent_result.refused}; {reason}"

    result = TaskResult(
        task_id=task.id,
        run_id=agent_result.run_id,
        passed=final_passed,
        reason=reason,
        expected=task.expected,
        refused=agent_result.refused,
        trace_dir=agent_result.trace_dir,
        verifier_evidence=evidence,
        final_text_excerpt=(agent_result.final_text or "")[:400],
    )

    # Write to disk BEFORE anything else observes it (brief §8: every run written before scoring).
    (task_dir / "result.json").write_text(
        result.model_dump_json(indent=2), encoding="utf-8"
    )
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(task.model_dump(), sort_keys=False), encoding="utf-8"
    )
    (task_dir / "summary.json").write_text(
        json.dumps(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "task_id": task.id,
                "run_id": agent_result.run_id,
                "passed": final_passed,
                "expected": task.expected,
                "refused": agent_result.refused,
                "verifier": task.verifier.fn,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return result


def _expected_matches(expected: str, verifier_passed: bool, refused: bool) -> bool:
    if expected == "refuse":
        return refused
    if expected == "produce":
        return not refused
    return True
