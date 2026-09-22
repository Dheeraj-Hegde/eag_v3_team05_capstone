"""HTTP client for the glc_v5 gateway.

Contract: POST /v1/chat with messages + tools. See glc_v5/glc/llm_schemas.py.
"""

from __future__ import annotations

from typing import Any

import httpx


class GLCClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8111",
        *,
        provider: str = "gemini",
        model: str | None = None,
        timeout: float = 180.0,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/v1/chat"
        self._provider = provider
        self._model = model
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GLCClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = "auto",
        temperature: float = 0.2,
        max_tokens: int = 4096,
        agent: str | None = "stock-agent",
        session: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "messages": messages,
            "provider": self._provider,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if self._model:
            body["model"] = self._model
        if system:
            body["system"] = system
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice
        if agent:
            body["agent"] = agent
        if session:
            body["session"] = session

        r = self._client.post(self._url, json=body)
        r.raise_for_status()
        return r.json()
