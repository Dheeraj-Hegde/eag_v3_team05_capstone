"""CLI entry point for the stock agent."""

from __future__ import annotations

import json
from typing import Optional

import typer

from stock_agent.agent import Agent, AgentConfig
from stock_agent.mcp_client import MCPClient
from stock_agent.tools import mcp_tools_to_glc

app = typer.Typer(add_completion=False, help="Stock (Seat 05) agent CLI.")


@app.command()
def run(
    prompt: str = typer.Option(..., "--prompt", "-p", help="User prompt for the agent."),
    task_id: Optional[str] = typer.Option(None, "--task-id", help="Optional task id for trace naming."),
    max_steps: int = typer.Option(20, "--max-steps"),
) -> None:
    cfg = AgentConfig.from_env()
    cfg.max_steps = max_steps
    agent = Agent(cfg)
    result = agent.run(prompt, task_id=task_id)
    typer.echo(f"[run_id]     {result.run_id}")
    typer.echo(f"[trace_dir]  {result.trace_dir}")
    typer.echo(f"[refused]    {result.refused}")
    typer.echo(f"[steps]      {result.steps}")
    typer.echo(f"[tool_calls] {len(result.tool_calls)}")
    typer.echo(f"[escalations]{len(result.escalations)}")
    typer.echo("")
    typer.echo(result.final_text)


@app.command("list-tools")
def list_tools() -> None:
    """Dump the filtered tool catalogue the agent would see."""
    cfg = AgentConfig.from_env()
    with MCPClient(cfg.as_base_url, cfg.as_token) as mcp:
        mcp.initialize()
        raw = mcp.list_tools()
    filtered = mcp_tools_to_glc(raw)
    typer.echo(f"raw MCP tools:      {len(raw)}")
    typer.echo(f"agent-visible tools:{len(filtered)} (including synthetic escalation.draft)")
    for t in filtered:
        typer.echo(f"  - {t['name']}")


@app.command()
def ping() -> None:
    """Verify AS_TOKEN, MCP handshake, and glc_v5 gateway are all reachable."""
    from stock_agent.glc_client import GLCClient

    cfg = AgentConfig.from_env()
    with MCPClient(cfg.as_base_url, cfg.as_token) as mcp:
        info = mcp.initialize()
        n = len(mcp.list_tools())
    typer.echo(f"[MCP] initialize ok. server_info={json.dumps(info)[:200]}")
    typer.echo(f"[MCP] tools/list  ok. count={n}")

    with GLCClient(cfg.glc_base_url, provider=cfg.glc_provider, model=cfg.glc_model) as glc:
        r = glc.chat([{"role": "user", "content": "reply with the single word: pong"}], temperature=0.0, max_tokens=8)
    typer.echo(f"[GLC] provider={r.get('provider')} model={r.get('model')} text={r.get('text','').strip()!r}")


if __name__ == "__main__":
    app()
