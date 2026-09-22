"""JSON-RPC 2.0 client for the AgentSwitch MCP endpoint.

Errors return HTTP 200 with a JSON-RPC error envelope. Only 401 fails at HTTP.
No SSE, no batching. Protocol version 2025-11-25.
"""

from __future__ import annotations

import itertools
from typing import Any

import httpx


class MCPError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


class MCPClient:
    PROTOCOL_VERSION = "2025-11-25"

    def __init__(self, base_url: str, token: str, *, timeout: float = 60.0) -> None:
        self._url = f"{base_url.rstrip('/')}/api/mcp"
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        self._ids = itertools.count(1)
        self._initialized = False

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "MCPClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
            "params": params or {},
        }
        r = self._client.post(self._url, json=payload)
        r.raise_for_status()
        env = r.json()
        if "error" in env:
            err = env["error"]
            raise MCPError(err.get("code", -1), err.get("message", "unknown"), err.get("data"))
        return env.get("result")

    def initialize(self, client_name: str = "stock-agent", client_version: str = "0.1") -> dict[str, Any]:
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": self.PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": client_name, "version": client_version},
            },
        )
        # notifications/initialized is fire-and-forget per the MCP spec
        self._client.post(
            self._url,
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        )
        self._initialized = True
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._rpc("tools/list", {})
        return result.get("tools", [])

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
