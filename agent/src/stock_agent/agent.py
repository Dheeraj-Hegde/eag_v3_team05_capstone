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

from stock_agent.balance import STOCK_BALANCE_TOOL_NAME, stock_balance
from stock_agent.llm_client import LLMClient
from stock_agent.mcp_client import MCPClient, MCPError
from stock_agent.tools import (
    ESCALATION_TOOL_NAME,
    HIDDEN_ARGUMENTS,
    from_llm_name,
    is_action,
    is_allowed,
    mcp_tools_to_llm,
    project_list_result,
)
from stock_agent.trace import Trace, new_run_id

REFUSAL_TOKEN = "REFUSE:"
MAX_TOOL_RESULT_CHARS = 6_000
# Bookkeeping fields that cost tokens on every row and never help the model.
_NOISE_KEYS = frozenset({"created_at", "updated_at", "created_by", "updated_by"})
FINAL_ANSWER_NUDGE = (
    "Budget reached: no more reads will be executed. If the task still needs its "
    "one write (e.g. StockEntry.create) or escalation.draft, make exactly that call "
    "now. In the same message, write your final answer from what you already have "
    "(or start with REFUSE: if that applies)."
)
FINAL_TEXT_ONLY = "Write your final answer now in plain text. Do not call any tools."


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
    max_steps: int = 6
    temperature: float = 0.2
    deadline_s: float = 300.0
    # Prompt + completion tokens one run may spend across all model calls.
    token_budget: int = 40_000
    max_calls_per_tool: int = 3

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
    tokens_used: int = 0


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


def _compact(obj: Any) -> Any:
    """Drop empty values, `_`-private keys and audit fields. Keeps 0 and False."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if k.startswith("_") or k in _NOISE_KEYS:
                continue
            v = _compact(v)
            if v is None or v == "" or v == [] or v == {}:
                continue
            out[k] = v
        return out
    if isinstance(obj, list):
        return [c for c in (_compact(v) for v in obj) if c != {}]
    return obj


def _action_note(name: str, out: dict[str, Any]) -> str:
    if not out.get("ok"):
        return f"{name} failed"
    res = out.get("result")
    if isinstance(res, dict) and (res.get("number") or res.get("id")):
        return f"{name} -> {res.get('number') or res.get('id')}"
    return f"{name} -> recorded"


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, separators=(",", ":"), ensure_ascii=False)


def _truncate_json(obj: Any, limit: int = MAX_TOOL_RESULT_CHARS) -> str:
    obj = _compact(obj)
    s = _dumps(obj)
    if len(s) <= limit:
        return s
    # Cut list results on whole rows and say so; a mid-row cut makes models re-query.
    res = obj.get("result") if isinstance(obj, dict) else None
    key = next((k for k in ("data", "rows") if isinstance(res, dict) and isinstance(res.get(k), list)), None)
    if key:
        rows = res[key]
        total = len(rows)
        lo, hi = 0, total
        while lo < hi:
            mid = (lo + hi + 1) // 2
            trial = dict(obj, result=dict(res, **{key: rows[:mid]}))
            if len(_dumps(trial)) + 120 <= limit:
                lo = mid
            else:
                hi = mid - 1
        trimmed = dict(obj, result=dict(res, **{key: rows[:lo]}))
        trimmed["note"] = f"showing {lo} of {total} rows returned; narrow with search/filters to see others"
        return _dumps(trimmed)
    return s[:limit] + f"... [truncated {len(s) - limit} chars; use filters or a smaller limit]"


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
                last_in = 0  # prompt tokens of the previous call
                pending_chars = 0  # chars appended since the previous call
                call_counts: dict[str, int] = {}

                for step in range(1, self.cfg.max_steps + 1):
                    if time.monotonic() > deadline_at:
                        result.stopped_reason = "deadline"
                        trace.event("deadline_reached", {"deadline_s": self.cfg.deadline_s})
                        break
                    result.steps = step

                    # Each call re-sends the whole conversation, so the next prompt
                    # costs roughly last prompt + whatever was appended since. Only
                    # make a normal call if a forced final call still fits after it.
                    projected_in = last_in + pending_chars // 3
                    forced: str | None = None
                    if step == self.cfg.max_steps:
                        forced = "max_steps"
                    elif last_in and result.tokens_used + 2 * projected_in + 4_000 > self.cfg.token_budget:
                        forced = "token_budget"
                    if forced:
                        trace.event(
                            "final_only",
                            {"step": step, "reason": forced, "tokens_used": result.tokens_used, "projected_in": projected_in},
                        )
                        messages.append({"role": "user", "content": FINAL_ANSWER_NUDGE})

                    trace.event("llm_request", {"step": step, "messages_len": len(messages)})
                    resp = llm.chat(
                        messages,
                        tools=tools,
                        temperature=self.cfg.temperature,
                        deadline=deadline_at,
                    )
                    usage = resp.get("usage") or {}
                    last_in = int(usage.get("input_tokens") or 0)
                    result.tokens_used += last_in + int(usage.get("output_tokens") or 0)
                    pending_chars = 0

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
                            "tokens_used": result.tokens_used,
                            **usage,
                        },
                    )
                    if forced and tool_calls:
                        # Never spend more tokens on reads here, but don't lose the task's
                        # actual action: run writes/escalations, then finish without the model.
                        actions = [tc for tc in tool_calls if is_action(from_llm_name(tc["name"], name_map))]
                        dropped = [tc["name"] for tc in tool_calls if tc not in actions]
                        if dropped:
                            trace.event("ignored_tool_calls", {"names": dropped})
                        notes = []
                        for tc in actions:
                            out = self._dispatch_tool_call(tc, name_map, mcp, trace, result, call_counts)
                            notes.append(_action_note(from_llm_name(tc["name"], name_map), out))
                        if notes and not text.strip():
                            text = "Actions taken: " + "; ".join(notes)
                        tool_calls = []
                        # A blank answer fails the task, so this one call may overrun the task
                        # budget by 25%; the harness charges real usage against the run budget.
                        if not text.strip() and result.tokens_used + last_in <= self.cfg.token_budget * 1.25:
                            # Model only asked for (dropped) reads; one text-only call to get an answer.
                            messages.append(resp["assistant_message"])
                            for tc in resp.get("tool_calls") or []:
                                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": '{"ok":false,"error":"not executed: budget reached"}'})
                            messages.append({"role": "user", "content": FINAL_TEXT_ONLY})
                            rescue = llm.chat(messages, tools=tools, tool_choice="none", temperature=self.cfg.temperature, deadline=deadline_at)
                            rusage = rescue.get("usage") or {}
                            result.tokens_used += int(rusage.get("input_tokens") or 0) + int(rusage.get("output_tokens") or 0)
                            text = rescue.get("text", "") or ""
                            trace.event("final_text_only", {"tokens_used": result.tokens_used, "text_chars": len(text)})

                    if not tool_calls:
                        if stop_reason == "length" and not continued and not forced:
                            # Reasoning models can burn the token budget on hidden thinking; ask once to finish.
                            continued = True
                            partial_text = text
                            trace.event("length_continue", {"step": step, "partial_chars": len(text)})
                            nudge = "Your reply was cut off. Continue exactly where you stopped and finish the final answer concisely."
                            messages.append({"role": "assistant", "content": text or ""})
                            messages.append({"role": "user", "content": nudge})
                            pending_chars = len(text) + len(nudge)
                            continue
                        text = partial_text + text if partial_text else text
                        result.final_text = text
                        result.stopped_reason = forced or stop_reason
                        result.refused = is_refusal(text)
                        trace.event("refusal" if result.refused else "final_answer", {"text": text})
                        break

                    partial_text = ""
                    messages.append(resp["assistant_message"])
                    for tc in tool_calls:
                        tool_output = self._dispatch_tool_call(tc, name_map, mcp, trace, result, call_counts)
                        content = _truncate_json(tool_output)
                        pending_chars += len(content)
                        messages.append({"role": "tool", "tool_call_id": tc["id"], "content": content})
                else:
                    result.stopped_reason = "max_steps"
                    trace.event("max_steps_reached", {"max_steps": self.cfg.max_steps})
        except Exception as e:
            result.stopped_reason = f"error: {type(e).__name__}"
            trace.event("agent_error", {"type": type(e).__name__, "message": str(e)[:2000]})
            # Lets the harness charge tokens already spent against its run budget.
            setattr(e, "tokens_used", result.tokens_used)
            raise
        finally:
            trace.set_meta(
                steps=result.steps,
                stopped_reason=result.stopped_reason,
                tool_call_count=len(result.tool_calls),
                escalation_count=len(result.escalations),
                blocked_call_count=len(result.blocked_calls),
                tokens_used=result.tokens_used,
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
        call_counts: dict[str, int] | None = None,
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

        if call_counts is not None:
            n = call_counts.get(name, 0)
            if n >= self.cfg.max_calls_per_tool:
                # Loops like paging through Item.list 12x were the main token sink.
                trace.event("repeat_guard", {"name": name, "count": n})
                return {
                    "ok": False,
                    "error": (
                        f"Call limit reached: {name} was already called {n} times in this run. "
                        "Do not call it again; answer from the results you already have."
                    ),
                }
            call_counts[name] = n + 1

        if name == STOCK_BALANCE_TOOL_NAME:
            return self._stock_balance(tc, args, mcp, trace, result)

        hidden = HIDDEN_ARGUMENTS.get(name, frozenset()) & set(args)
        if hidden:
            args = {k: v for k, v in args.items() if k not in hidden}
            trace.event("stripped_arguments", {"name": name, "arguments": sorted(hidden)})

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
            return {"ok": True, "result": project_list_result(name, payload)}
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

    def _stock_balance(
        self,
        tc: dict[str, Any],
        args: dict[str, Any],
        mcp: MCPClient,
        trace: Trace,
        result: AgentResult,
    ) -> dict[str, Any]:
        def call(name: str, call_args: dict[str, Any]) -> Any:
            rec: dict[str, Any] = {"id": tc.get("id"), "name": name, "arguments": call_args, "via": STOCK_BALANCE_TOOL_NAME}
            try:
                out = mcp.call_tool(name, call_args)
            except (MCPError, httpx.HTTPError) as e:
                rec["error"] = {"message": str(e)[:300]}
                result.tool_calls.append(rec)
                raise
            payload = tool_result_payload(out)
            if isinstance(out, dict) and out.get("isError"):
                rec["error"] = {"message": "isError", "data": payload}
                result.tool_calls.append(rec)
                raise MCPError(-1, f"{name} returned an error", payload)
            result.tool_calls.append(rec)
            return payload

        trace.event("stock_balance_call", {"id": tc.get("id"), "arguments": args})
        try:
            out = stock_balance(call, args)
        except (MCPError, httpx.HTTPError) as e:
            trace.event("stock_balance_error", {"id": tc.get("id"), "message": str(e)[:500]})
            return {"ok": False, "error": f"stock.balance failed: {e}"}
        trace.event("stock_balance_result", {"id": tc.get("id"), "rows": out.get("row_count")})
        return {"ok": True, "result": out}
