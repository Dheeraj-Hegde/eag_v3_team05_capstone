"""Stock agent tool-calling loop.

Talks to glc_v5 for the LLM turn, to AgentSwitch MCP for tool execution, and
writes every event to disk via Trace. Off-seat writes are captured via the
synthetic `escalation.draft` tool and never hit MCP.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel

from stock_agent.glc_client import GLCClient
from stock_agent.mcp_client import MCPClient, MCPError
from stock_agent.tools import (
    ESCALATION_TOOL_NAME,
    is_allowed,
    mcp_tools_to_glc,
)
from stock_agent.trace import Trace, new_run_id

REFUSAL_TOKEN = "REFUSE:"


class AgentConfig(BaseModel):
    as_base_url: str
    as_token: str
    glc_base_url: str = "http://127.0.0.1:8111"
    glc_provider: str = "gemini"
    glc_model: str | None = None
    trace_dir: str = "./traces"
    max_steps: int = 20
    temperature: float = 0.2

    @classmethod
    def from_env(cls, *, env_file: str | os.PathLike[str] | None = ".env") -> "AgentConfig":
        if env_file:
            load_dotenv(env_file)
        token = os.environ.get("AS_TOKEN", "")
        if not token:
            raise RuntimeError(
                "AS_TOKEN is empty. Log in and set it in .env or the environment."
            )
        return cls(
            as_base_url=os.environ["AS_BASE_URL"],
            as_token=token,
            glc_base_url=os.environ.get("GLC_BASE_URL", "http://127.0.0.1:8111"),
            glc_provider=os.environ.get("GLC_PROVIDER", "gemini"),
            glc_model=os.environ.get("GLC_MODEL") or None,
            trace_dir=os.environ.get("TRACE_DIR", "./traces"),
        )


@dataclass
class AgentResult:
    run_id: str
    trace_dir: str
    final_text: str
    refused: bool
    stopped_reason: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    escalations: list[dict[str, Any]] = field(default_factory=list)
    steps: int = 0


class Agent:
    def __init__(self, cfg: AgentConfig) -> None:
        self.cfg = cfg
        self._system_prompt = self._load_system_prompt()

    @staticmethod
    def _load_system_prompt() -> str:
        return (Path(__file__).parent / "system_prompt.md").read_text(encoding="utf-8")

    def get_tools(self, mcp: MCPClient) -> list[dict[str, Any]]:
        raw = mcp.list_tools()
        return mcp_tools_to_glc(raw)

    def run(self, prompt: str, *, task_id: str | None = None) -> AgentResult:
        run_id = new_run_id(task_id)
        trace = Trace(self.cfg.trace_dir, run_id, task_id=task_id)
        trace.event("prompt", {"text": prompt})

        with MCPClient(self.cfg.as_base_url, self.cfg.as_token) as mcp, GLCClient(
            self.cfg.glc_base_url,
            provider=self.cfg.glc_provider,
            model=self.cfg.glc_model,
        ) as glc:
            mcp.initialize()
            tools = self.get_tools(mcp)
            trace.event("tools_loaded", {"count": len(tools)})

            messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
            result = AgentResult(
                run_id=run_id,
                trace_dir=str(trace.dir),
                final_text="",
                refused=False,
                stopped_reason="unknown",
            )

            for step in range(1, self.cfg.max_steps + 1):
                result.steps = step
                trace.event("glc_request", {"step": step, "messages_len": len(messages)})
                resp = glc.chat(
                    messages,
                    system=self._system_prompt,
                    tools=tools,
                    temperature=self.cfg.temperature,
                    session=run_id,
                )
                text = resp.get("text", "") or ""
                tool_calls = resp.get("tool_calls", []) or []
                stop_reason = resp.get("stop_reason", "end_turn")
                trace.event(
                    "glc_response",
                    {
                        "step": step,
                        "stop_reason": stop_reason,
                        "text_chars": len(text),
                        "tool_calls": len(tool_calls),
                        "input_tokens": resp.get("input_tokens", 0),
                        "output_tokens": resp.get("output_tokens", 0),
                    },
                )

                if not tool_calls:
                    result.final_text = text
                    result.stopped_reason = stop_reason
                    result.refused = text.lstrip().startswith(REFUSAL_TOKEN)
                    if result.refused:
                        trace.event("refusal", {"text": text})
                    else:
                        trace.event("final_answer", {"text": text})
                    break

                messages.append(
                    {"role": "assistant", "content": text, "tool_calls": tool_calls}
                )
                for tc in tool_calls:
                    tool_output = self._dispatch_tool_call(tc, mcp, trace, result)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": json.dumps(tool_output, default=str),
                        }
                    )
            else:
                result.stopped_reason = "max_steps"
                trace.event("max_steps_reached", {"max_steps": self.cfg.max_steps})

            trace.set_meta(
                steps=result.steps,
                tool_call_count=len(result.tool_calls),
                escalation_count=len(result.escalations),
            )
            trace.close(final_answer=result.final_text, refused=result.refused)
            return result

    def _dispatch_tool_call(
        self,
        tc: dict[str, Any],
        mcp: MCPClient,
        trace: Trace,
        result: AgentResult,
    ) -> dict[str, Any]:
        name = tc.get("name", "")
        args = tc.get("arguments", {}) or {}

        if name == ESCALATION_TOOL_NAME:
            record = {"id": tc.get("id"), "arguments": args}
            result.escalations.append(record)
            trace.event("escalation", record)
            return {"ok": True, "recorded": True, "note": "Captured off-seat write intent."}

        if not is_allowed(name):
            trace.event("blocked_tool_call", {"name": name, "reason": "not in allowlist"})
            return {
                "ok": False,
                "error": (
                    f"Tool '{name}' is not permitted from this seat. "
                    "Use escalation.draft for off-seat writes."
                ),
            }

        record: dict[str, Any] = {"id": tc.get("id"), "name": name, "arguments": args}
        try:
            trace.event("mcp_call", record)
            out = mcp.call_tool(name, args)
            record["result"] = out
            result.tool_calls.append(record)
            trace.event("mcp_result", {"id": tc.get("id"), "name": name})
            return {"ok": True, "result": out}
        except MCPError as e:
            record["error"] = {"code": e.code, "message": e.message, "data": e.data}
            result.tool_calls.append(record)
            trace.event("mcp_error", {"id": tc.get("id"), "name": name, "code": e.code, "message": e.message})
            return {"ok": False, "error": {"code": e.code, "message": e.message, "data": e.data}}
