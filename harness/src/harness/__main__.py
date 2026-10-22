"""Official harness entrypoint: `python -m harness`.

This is what the AgentSwitch runner executes (see agentswitch-harness.toml).
It runs every task in harness/tasks once against $AGENTSWITCH_INSTANCE, and
always leaves a valid results.json behind — even on config errors, crashes or
a looming timeout. Use `python -m harness --validate results.json` to check a
file before submitting.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from pathlib import Path

from harness.results import build_document, official_task, validate_file, write_document

DEFAULT_BUDGET_S = 17 * 60  # server timeout is 20 min; keep headroom for install/teardown
DEFAULT_TASK_DEADLINE_S = 180
MIN_TASK_WINDOW_S = 45
# Platform model limit is 300k tokens; stay well below it across all tasks.
DEFAULT_TOKEN_BUDGET = 200_000
DEFAULT_TASK_TOKEN_BUDGET = 40_000
MIN_TASK_TOKENS = 10_000


def _validate(path: str) -> int:
    errors = validate_file(path)
    if errors:
        print(f"INVALID {path}:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"OK {path} matches the AgentSwitch results.json format")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m harness", description=__doc__)
    parser.add_argument("--validate", metavar="PATH", help="validate an existing results.json and exit")
    parser.add_argument("--results", default=os.environ.get("HARNESS_RESULTS", "results.json"))
    parser.add_argument("--tasks-dir", default=None)
    parser.add_argument("--runs-root", default=os.environ.get("HARNESS_RUNS_ROOT", "runs"))
    parser.add_argument("--only", default="", help="comma-separated task ids, e.g. T01,T05")
    parser.add_argument("--budget-s", type=float, default=float(os.environ.get("HARNESS_BUDGET_S", DEFAULT_BUDGET_S)))
    parser.add_argument(
        "--task-deadline-s",
        type=float,
        default=float(os.environ.get("HARNESS_TASK_DEADLINE_S", DEFAULT_TASK_DEADLINE_S)),
    )
    parser.add_argument(
        "--token-budget",
        type=int,
        default=int(os.environ.get("HARNESS_TOKEN_BUDGET", DEFAULT_TOKEN_BUDGET)),
        help="max model tokens (prompt+completion) for the whole run",
    )
    parser.add_argument(
        "--task-token-budget",
        type=int,
        default=int(os.environ.get("HARNESS_TASK_TOKEN_BUDGET", DEFAULT_TASK_TOKEN_BUDGET)),
        help="max model tokens for a single task",
    )
    args = parser.parse_args(argv)

    # Agent text/reasons may contain non-ASCII; never let a console codec kill the run.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    if args.validate:
        return _validate(args.validate)

    started = time.monotonic()
    results_path = Path(args.results)
    instance = os.environ.get("AGENTSWITCH_INSTANCE")
    model = os.environ.get("OPENAI_MODEL")

    # Imports that pull in the agent stay inside main so --validate works standalone.
    try:
        from harness.runner import load_task, run_task, task_files

        files = task_files(args.tasks_dir)
        tasks = [load_task(f) for f in files]
        if args.only:
            wanted = {t.strip() for t in args.only.split(",") if t.strip()}
            tasks = [t for t in tasks if t.id in wanted]
        if not tasks:
            raise RuntimeError(f"no tasks found (tasks_dir={args.tasks_dir or 'default'}, only={args.only or '-'})")
    except Exception as e:
        traceback.print_exc()
        write_document(
            build_document(
                [official_task("setup", "Harness setup", False, f"task loading failed: {type(e).__name__}: {e}")],
                instance=instance,
                model=model,
                note="setup failed",
            ),
            results_path,
        )
        return 0

    rows: dict[str, dict] = {
        t.id: official_task(t.id, t.title, False, "not run (harness stopped before this task)") for t in tasks
    }

    def flush(note: str = "") -> None:
        write_document(build_document(list(rows.values()), instance=instance, model=model, note=note), results_path)

    flush("in progress")

    try:
        from stock_agent.agent import AgentConfig

        cfg = AgentConfig.from_env()
    except Exception as e:
        traceback.print_exc()
        for t in tasks:
            rows[t.id] = official_task(t.id, t.title, False, f"config error: {e}")
        flush("config error")
        return 0

    print(
        f"[harness] instance={cfg.instance or '-'} model={cfg.llm_model} tasks={len(tasks)} "
        f"budget={args.budget_s:.0f}s tokens={args.token_budget}"
    )
    tokens_spent = 0
    for i, t in enumerate(tasks):
        remaining = args.budget_s - (time.monotonic() - started)
        if remaining < MIN_TASK_WINDOW_S:
            rows[t.id] = official_task(t.id, t.title, False, f"skipped: time budget exhausted ({remaining:.0f}s left)")
            print(f"[{t.id}] skipped - time budget exhausted")
            continue
        # Reserve a minimum for each later task so one heavy task can't starve the rest.
        tasks_after = len(tasks) - i - 1
        task_tokens = min(
            args.task_token_budget,
            args.token_budget - tokens_spent - MIN_TASK_TOKENS * tasks_after,
        )
        if task_tokens < MIN_TASK_TOKENS:
            rows[t.id] = official_task(
                t.id, t.title, False, f"skipped: token budget exhausted ({args.token_budget - tokens_spent} left)"
            )
            print(f"[{t.id}] skipped - token budget exhausted")
            continue
        deadline = min(args.task_deadline_s, remaining - 30)
        t0 = time.monotonic()
        print(f"[{t.id}] {t.title} (expected={t.expected}, deadline={deadline:.0f}s, tokens<={task_tokens})", flush=True)
        try:
            r = run_task(t, runs_root=args.runs_root, cfg=cfg, deadline_s=deadline, token_budget=task_tokens)
            tokens_spent += r.tokens_used
            evidence = r.reason
            if r.stopped_reason and r.stopped_reason not in ("stop", "end_turn"):
                evidence = f"[{r.stopped_reason}] {evidence}"
            rows[t.id] = official_task(t.id, t.title, r.passed, evidence, r.score)
            print(
                f"[{t.id}] passed={r.passed} ({time.monotonic() - t0:.0f}s, {r.tokens_used} tokens) {r.reason[:200]}",
                flush=True,
            )
        except Exception as e:
            tokens_spent += int(getattr(e, "tokens_used", 0) or 0)
            traceback.print_exc()
            rows[t.id] = official_task(t.id, t.title, False, f"crashed: {type(e).__name__}: {e}")
            print(f"[{t.id}] crashed: {type(e).__name__}: {e}", flush=True)
        flush(f"in progress | tokens={tokens_spent}")

    flush(f"tokens={tokens_spent}")
    passed = sum(1 for r in rows.values() if r["passed"])
    print(
        f"[harness] {passed}/{len(rows)} passed in {time.monotonic() - started:.0f}s, "
        f"{tokens_spent} tokens -> {results_path}"
    )
    # Exit 0 regardless: the panel scores from results.json, not the exit code.
    return 0


if __name__ == "__main__":
    sys.exit(main())
