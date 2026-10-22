"""CLI entry point for the stock agent."""

from __future__ import annotations

import json
from typing import Optional

import typer

from stock_agent.agent import Agent, AgentConfig
from stock_agent.mcp_client import MCPClient
from stock_agent.tools import is_allowed, mcp_tools_to_llm

app = typer.Typer(add_completion=False, help="Stock (Seat 05) agent CLI.")


@app.command()
def run(
    prompt: str = typer.Option(..., "--prompt", "-p", help="User prompt for the agent."),
    task_id: Optional[str] = typer.Option(None, "--task-id", help="Optional task id for trace naming."),
    max_steps: int = typer.Option(6, "--max-steps"),
    token_budget: int = typer.Option(40_000, "--token-budget", help="Max model tokens for this run."),
) -> None:
    cfg = AgentConfig.from_env()
    cfg.max_steps = max_steps
    cfg.token_budget = token_budget
    agent = Agent(cfg)
    result = agent.run(prompt, task_id=task_id)
    typer.echo(f"[run_id]     {result.run_id}")
    typer.echo(f"[trace_dir]  {result.trace_dir}")
    typer.echo(f"[refused]    {result.refused}")
    typer.echo(f"[steps]      {result.steps}")
    typer.echo(f"[tool_calls] {len(result.tool_calls)}")
    typer.echo(f"[escalations]{len(result.escalations)}")
    typer.echo(f"[blocked]    {len(result.blocked_calls)}")
    typer.echo(f"[stopped]    {result.stopped_reason}")
    typer.echo(f"[tokens]     {result.tokens_used}")
    typer.echo("")
    typer.echo(result.final_text)


@app.command("list-tools")
def list_tools() -> None:
    """Dump the filtered tool catalogue the agent would see."""
    cfg = AgentConfig.from_env()
    with MCPClient(cfg.as_base_url, cfg.as_token) as mcp:
        mcp.initialize()
        raw = mcp.list_tools()
    filtered, name_map = mcp_tools_to_llm(raw)
    seat_allowed = sum(1 for t in raw if is_allowed(t.get("name", "")))
    typer.echo(f"raw MCP tools:       {len(raw)}")
    typer.echo(f"seat-allowed tools:  {seat_allowed}")
    typer.echo(f"agent-visible tools: {len(filtered)} (inventory trim + synthetic escalation.draft)")
    for llm_name, mcp_name in name_map.items():
        typer.echo(f"  - {mcp_name}  (as {llm_name})")


@app.command()
def ping() -> None:
    """Verify the AgentSwitch token, MCP handshake, and the OpenAI-compatible model."""
    from stock_agent.llm_client import LLMClient

    cfg = AgentConfig.from_env()
    with MCPClient(cfg.as_base_url, cfg.as_token) as mcp:
        info = mcp.initialize()
        n = len(mcp.list_tools())
    typer.echo(f"[MCP] instance={cfg.instance or '-'} initialize ok. server_info={json.dumps(info)[:200]}")
    typer.echo(f"[MCP] tools/list  ok. count={n}")

    with LLMClient(cfg.llm_model, base_url=cfg.llm_base_url, api_key=cfg.llm_api_key) as llm:
        r = llm.chat([{"role": "user", "content": "reply with the single word: pong"}], temperature=0.0, max_tokens=16)
    typer.echo(f"[LLM] model={r.get('model')} text={r.get('text','').strip()!r}")


if __name__ == "__main__":
    app()
