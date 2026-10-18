"""Stock agent tool-calling loop.

Talks to an OpenAI-compatible model for the LLM turn, to AgentSwitch MCP for
tool execution, and writes every event to disk via Trace. Off-seat writes are
captured via the synthetic `escalation.draft` tool and never hit MCP.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel

from stock_agent.llm_client import LLMClient
from stock_agent.mcp_client import MCPClient, MCPError
from stock_agent.tools import (
    ESCALATION_TOOL_NAME,
    from_llm_name,
    is_allowed,
    mcp_tools_to_llm,
)
from stock_agent.trace import Trace, new_run_id

REFUSAL_TOKEN = "REFUSE:"
MAX_TOOL_RESULT_CHARS = 12_000


def _env(*names: str, default: str | None = None) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


def is_refusal(text: str) -> bool:
    # Tolerate markdown wrappers like "**REFUSE:**" or "`REFUSE:`".
    return text.lstrip().lstrip("*_`# >").startswith(REFUSAL_TOKEN)


class AgentConfig(BaseModel):
    as_base_url: str
    as_token: str
    instance: str | None = None
    llm_model: str
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    trace_dir: str = "./traces"
    max_steps: int = 20
    temperature: float = 0.2
    deadline_s: float = 300.0

    @classmethod
    def from_env(cls, *, env_file: str | os.PathLike[str] | None = ".env") -> "AgentConfig":
        """Server env names (AGENTSWITCH_*, OPENAI_*) win; AS_* is a local fallback."""
        if env_file and Path(env_file).is_file():
            load_dotenv(env_file)
        base_url = _env("AGENTSWITCH_BASE_URL", "AS_BASE_URL")
        token = _env("AGENTSWITCH_TOKEN", "AS_TOKEN")
        model = _env("OPENAI_MODEL")
        missing = [
            name
            for name, val in (
                ("AGENTSWITCH_BASE_URL", base_url),
                ("AGENTSWITCH_TOKEN", token),
                ("OPENAI_MODEL", model),
            )
            if not val
        ]
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
        return cls(
            as_base_url=base_url,
            as_token=token,
            instance=_env("AGENTSWITCH_INSTANCE"),
            llm_model=model,
            llm_base_url=_env("OPENAI_BASE_URL"),
            llm_api_key=_env("OPENAI_API_KEY"),
            trace_dir=_env("TRACE_DIR", default="./traces"),
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
    blocked_calls: list[dict[str, Any]] = field(default_factory=list)
    steps: int = 0


def tool_result_payload(out: Any) -> Any:
    """Prefer structuredContent; fall back to the text content."""
    if isinstance(out, dict):
        if out.get("structuredContent") is not None:
            return out["structuredContent"]
        content = out.get("content")
        if isinstance(content, list):
            texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            if texts:
                joined = "\n".join(texts)
                try:
                    return json.loads(joined)
                except json.JSONDecodeError:
                    return joined
    return out


def _truncate_json(obj: Any, limit: int = MAX_TOOL_RESULT_CHARS) -> str:
    s = json.dumps(obj, default=str)
    if len(s) <= limit:
        return s
    return s[:limit] + f'... [truncated {len(s) - limit} chars; narrow your query with filters/limit]'


class Agent:
    def __init__(self, cfg: AgentConfig) -> None:
        self.cfg = cfg
        self._system_prompt = self._load_system_prompt()

    @staticmethod
    def _load_system_prompt() -> str:
        return (Path(__file__).parent / "system_prompt.md").read_text(encoding="utf-8")

    def get_tools(self, mcp: MCPClient) -> tuple[list[dict[str, Any]], dict[str, str]]:
        return mcp_tools_to_llm(mcp.list_tools())

    def run(self, prompt: str, *, task_id: str | None = None) -> AgentResult:
        run_id = new_run_id(task_id)
        trace = Trace(self.cfg.trace_dir, run_id, task_id=task_id)
        trace.event("prompt", {"text": prompt, "instance": self.cfg.instance, "model": self.cfg.llm_model})
        started = time.monotonic()

        result = AgentResult(
            run_id=run_id,
            trace_dir=str(trace.dir),
            final_text="",
            refused=False,
            stopped_reason="unknown",
        )
        try:
            with MCPClient(self.cfg.as_base_url, self.cfg.as_token) as mcp, LLMClient(
                self.cfg.llm_model,
                base_url=self.cfg.llm_base_url,
                api_key=self.cfg.llm_api_key,
            ) as llm:
                mcp.initialize()
                tools, name_map = self.get_tools(mcp)
                trace.event("tools_loaded", {"count": len(tools)})

                messages: list[dict[str, Any]] = [
                    {"role": "system", "content": self._system_prompt},
                    {"role": "user", "content": prompt},
                ]
                deadline_at = started + self.cfg.deadline_s
                partial_text = ""
                continued = False

                for step in range(1, self.cfg.max_steps + 1):
                    if time.monotonic() > deadline_at:
                        result.stopped_reason = "deadline"
                        trace.event("deadline_reached", {"deadline_s": self.cfg.deadline_s})
                        break
                    result.steps = step
                    trace.event("llm_request", {"step": step, "messages_len": len(messages)})
                    resp = llm.chat(
                        messages,
                        tools=tools,
                        temperature=self.cfg.temperature,
                        deadline=deadline_at,
                    )
                    text = resp.get("text", "") or ""
                    tool_calls = resp.get("tool_calls", []) or []
                    stop_reason = resp.get("stop_reason", "stop")
                    trace.event(
                        "llm_response",
                        {
                            "step": step,
                            "stop_reason": stop_reason,
                            "text_chars": len(text),
                            "tool_calls": [tc["name"] for tc in tool_calls],
                            **resp.get("usage", {}),
                        },
                    )

                    if not tool_calls:
                        if stop_reason == "length" and not continued:
                            # Reasoning models can burn the token budget on hidden thinking; ask once to finish.
                            continued = True
                            partial_text = text
                            trace.event("length_continue", {"step": step, "partial_chars": len(text)})
                            messages.append({"role": "assistant", "content": text or ""})
                            messages.append(
                                {
                                    "role": "user",
                                    "content": "Your reply was cut off. Continue exactly where you stopped and finish the final answer concisely.",
                                }
                            )
                            continue
                        text = partial_text + text if partial_text else text
                        result.final_text = text
                        result.stopped_reason = stop_reason
                        result.refused = is_refusal(text)
                        trace.event("refusal" if result.refused else "final_answer", {"text": text})
                        break

                    partial_text = ""
                    messages.append(resp["assistant_message"])
                    for tc in tool_calls:
                        tool_output = self._dispatch_tool_call(tc, name_map, mcp, trace, result)
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "content": _truncate_json(tool_output),
                            }
                        )
                else:
                    result.stopped_reason = "max_steps"
                    trace.event("max_steps_reached", {"max_steps": self.cfg.max_steps})
        except Exception as e:
            result.stopped_reason = f"error: {type(e).__name__}"
            trace.event("agent_error", {"type": type(e).__name__, "message": str(e)[:2000]})
            raise
        finally:
            trace.set_meta(
                steps=result.steps,
                stopped_reason=result.stopped_reason,
                tool_call_count=len(result.tool_calls),
                escalation_count=len(result.escalations),
                blocked_call_count=len(result.blocked_calls),
            )
            trace.close(final_answer=result.final_text, refused=result.refused)
        return result

    def _dispatch_tool_call(
        self,
        tc: dict[str, Any],
        name_map: dict[str, str],
        mcp: MCPClient,
        trace: Trace,
        result: AgentResult,
    ) -> dict[str, Any]:
        name = from_llm_name(tc.get("name", ""), name_map)
        args = tc.get("arguments", {}) or {}

        if tc.get("parse_error"):
            trace.event("bad_tool_arguments", {"name": name, "error": tc["parse_error"]})
            return {"ok": False, "error": f"Arguments were not valid JSON: {tc['parse_error']}"}

        if name == ESCALATION_TOOL_NAME:
            record = {"id": tc.get("id"), "arguments": args}
            result.escalations.append(record)
            trace.event("escalation", record)
            return {"ok": True, "recorded": True, "note": "Captured off-seat write intent."}

        if not is_allowed(name):
            blocked = {"id": tc.get("id"), "name": name, "arguments": args}
            result.blocked_calls.append(blocked)
            trace.event("blocked_tool_call", {"name": name, "reason": "not in allowlist"})
            return {
                "ok": False,
                "error": (
                    f"Tool '{name}' is not permitted from this seat. "
                    "Use escalation.draft for off-seat writes, or refuse."
                ),
            }

        record: dict[str, Any] = {"id": tc.get("id"), "name": name, "arguments": args}
        try:
            trace.event("mcp_call", record)
            out = mcp.call_tool(name, args)
            record["result"] = out
            payload = tool_result_payload(out)
            if isinstance(out, dict) and out.get("isError"):
                record["error"] = {"code": None, "message": "isError", "data": payload}
                result.tool_calls.append(record)
                trace.event("mcp_error", {"id": tc.get("id"), "name": name, "is_error": True})
                return {"ok": False, "error": payload}
            result.tool_calls.append(record)
            trace.event("mcp_result", {"id": tc.get("id"), "name": name})
            return {"ok": True, "result": payload}
        except MCPError as e:
            record["error"] = {"code": e.code, "message": e.message, "data": e.data}
            result.tool_calls.append(record)
            trace.event("mcp_error", {"id": tc.get("id"), "name": name, "code": e.code, "message": e.message})
            return {"ok": False, "error": {"code": e.code, "message": e.message, "data": e.data}}
        except httpx.HTTPError as e:
            record["error"] = {"code": None, "message": f"{type(e).__name__}: {e}", "data": None}
            result.tool_calls.append(record)
            trace.event("mcp_http_error", {"id": tc.get("id"), "name": name, "message": str(e)[:500]})
            return {"ok": False, "error": f"Transport error calling {name}: {e}"}
