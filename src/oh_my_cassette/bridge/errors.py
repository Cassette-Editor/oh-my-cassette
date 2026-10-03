"""Errors the bridge itself reports to the host (contract §6)."""

from __future__ import annotations

import mcp_types as types


class BridgeError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def result(self) -> types.CallToolResult:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"{self.code}: {self.message}")],
            structured_content={
                "error": {"code": self.code, "message": self.message, "retryable": self.retryable}
            },
            is_error=True,
        )


def unauthorized(url: str, *, has_token: bool) -> BridgeError:
    if has_token:
        message = (
            f"{url} rejected CASSETTE_AUTH_TOKEN (HTTP 401); set a valid token and restart this MCP server"
        )
    else:
        message = f"{url} requires a token (HTTP 401); set CASSETTE_AUTH_TOKEN and restart this MCP server"
    return BridgeError("bridge.unauthorized", message)


def protocol_unsupported(message: str) -> BridgeError:
    return BridgeError("bridge.protocol_unsupported", f"{message}; the backend must be upgraded")


def unreachable(url: str, detail: str, *, during_call: bool) -> BridgeError:
    if during_call:
        message = (
            f"the connection to {url} broke during the call ({detail}). The backend's work may still be "
            "running; call the tool again with the same arguments to pick it up."
        )
    else:
        message = f"{url}: {detail}"
    return BridgeError("bridge.backend_unreachable", message, retryable=True)
