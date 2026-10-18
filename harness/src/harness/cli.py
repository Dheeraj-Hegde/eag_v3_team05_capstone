"""Harness CLI."""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from harness.runner import default_tasks_dir, load_task, run_task, task_files
from harness.scorer import collect_latest_results, exit_code, render_table

app = typer.Typer(add_completion=False, help="Stock agent harness: run tasks, score results.")


@app.command()
def run(
    task_path: str = typer.Argument(..., help="Path to a task YAML."),
    runs_root: str = typer.Option("runs", "--runs-root"),
) -> None:
    task = load_task(task_path)
    typer.echo(f"[task] {task.id} — {task.title}")
    typer.echo(f"[expected] {task.expected}")
    result = run_task(task, runs_root=runs_root)
    typer.echo("")
    typer.echo(f"[run_id]    {result.run_id}")
    typer.echo(f"[trace_dir] {result.trace_dir}")
    typer.echo(f"[refused]   {result.refused}")
    typer.echo(f"[passed]    {result.passed}")
    typer.echo(f"[reason]    {result.reason}")
    raise typer.Exit(code=0 if result.passed else 1)


@app.command("run-all")
def run_all(
    tasks_dir: str = typer.Option(None, "--tasks-dir", help="Defaults to harness/tasks."),
    runs_root: str = typer.Option("runs", "--runs-root"),
) -> None:
    files = task_files(tasks_dir)
    if not files:
        typer.echo(f"no tasks found under {tasks_dir or default_tasks_dir()}/")
        raise typer.Exit(code=1)
    for tf in files:
        task = load_task(tf)
        typer.echo(f"→ running {task.id} — {task.title}")
        result = run_task(task, runs_root=runs_root)
        typer.echo(f"  passed={result.passed} reason={result.reason}")
    results = collect_latest_results(runs_root)
    typer.echo("")
    typer.echo(render_table(results))
    raise typer.Exit(code=exit_code(results))


@app.command()
def score(runs_root: str = typer.Option("runs", "--runs-root")) -> None:
    results = collect_latest_results(runs_root)
    typer.echo(render_table(results))
    raise typer.Exit(code=exit_code(results))


@app.command("list-tasks")
def list_tasks(tasks_dir: str = typer.Option(None, "--tasks-dir", help="Defaults to harness/tasks.")) -> None:
    for tf in task_files(tasks_dir):
        task = load_task(tf)
        typer.echo(f"{task.id:<6} {task.expected:<8} {task.title}")


if __name__ == "__main__":
    app()
