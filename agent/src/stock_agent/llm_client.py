"""OpenAI-compatible chat client.

`OpenAI()` picks up OPENAI_BASE_URL and OPENAI_API_KEY from the environment, so
the same code runs against the AgentSwitch harness model and any local
OpenAI-compatible endpoint.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

import openai
from openai import OpenAI

_RETRYABLE = (
    openai.RateLimitError,
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.InternalServerError,
)
_RETRY_IN = re.compile(r"retry in ([\d.]+)s", re.IGNORECASE)
# Message-level fields some servers reject when sent back as input.
_DROP_ON_REPLAY = ("reasoning_content", "reasoning", "refusal", "annotations", "audio", "function_call")


def _retry_delay(err: Exception, attempt: int) -> float:
    m = _RETRY_IN.search(str(err))
    if m:
        return float(m.group(1)) + 1.0
    headers = getattr(getattr(err, "response", None), "headers", None) or {}
    ra = headers.get("retry-after") if hasattr(headers, "get") else None
    try:
        if ra:
            return float(ra) + 0.5
    except ValueError:
        pass
    return min(2.0 ** attempt, 30.0)


class LLMClient:
    def __init__(
        self,
        model: str,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 120.0,
        max_retries: int = 2,
    ) -> None:
        self._model = model
        self._client = OpenAI(
            base_url=base_url or None,
            api_key=api_key or None,
            timeout=timeout,
            max_retries=max_retries,
        )
        # Some models reject temperature/max_tokens; drop them after the first 400.
        self._send_sampling_params = True

    @property
    def model(self) -> str:
        return self._model

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 8192,
        deadline: float | None = None,
        max_attempts: int = 8,
    ) -> dict[str, Any]:
        """One completion. Retries 429/5xx/transport errors with backoff until
        `deadline` (a time.monotonic() value) or `max_attempts`."""
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._chat_once(messages, tools=tools, temperature=temperature, max_tokens=max_tokens)
            except _RETRYABLE as e:
                delay = _retry_delay(e, attempt)
                out_of_time = deadline is not None and time.monotonic() + delay > deadline
                if attempt >= max_attempts or out_of_time:
                    raise
                time.sleep(delay)

    def _chat_once(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"model": self._model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if self._send_sampling_params:
            kwargs["temperature"] = temperature
            kwargs["max_tokens"] = max_tokens

        try:
            resp = self._client.chat.completions.create(**kwargs)
        except openai.BadRequestError as e:
            msg = str(e).lower()
            if self._send_sampling_params and ("temperature" in msg or "max_tokens" in msg):
                self._send_sampling_params = False
                kwargs.pop("temperature", None)
                kwargs.pop("max_tokens", None)
                resp = self._client.chat.completions.create(**kwargs)
            else:
                raise

        if not resp.choices:
            return {"text": "", "tool_calls": [], "stop_reason": "empty", "usage": {}}

        choice = resp.choices[0]
        msg = choice.message
        tool_calls: list[dict[str, Any]] = []
        for tc in msg.tool_calls or []:
            fn = getattr(tc, "function", None)
            if fn is None:
                continue
            if not tc.id:
                tc.id = f"call_{uuid.uuid4().hex[:12]}"
            raw_args = fn.arguments or "{}"
            try:
                args = json.loads(raw_args)
                if not isinstance(args, dict):
                    args = {}
                parse_error = None
            except json.JSONDecodeError as e:
                args = {}
                parse_error = str(e)
            tool_calls.append(
                {
                    "id": tc.id,
                    "name": fn.name,
                    "arguments": args,
                    "raw_arguments": raw_args,
                    "parse_error": parse_error,
                }
            )

        # Replay the assistant turn exactly as returned so provider extras survive
        # (e.g. Gemini's tool_calls[].extra_content.google.thought_signature).
        assistant_message = msg.model_dump(exclude_none=True)
        for k in _DROP_ON_REPLAY:
            assistant_message.pop(k, None)
        assistant_message["role"] = "assistant"
        if "content" not in assistant_message:
            assistant_message["content"] = None

        usage = resp.usage
        return {
            "text": msg.content or "",
            "tool_calls": tool_calls,
            "assistant_message": assistant_message,
            "stop_reason": choice.finish_reason or "stop",
            "model": resp.model,
            "usage": {
                "input_tokens": getattr(usage, "prompt_tokens", 0) if usage else 0,
                "output_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
            },
        }
