"""Envelope helpers shared by every tool."""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.mcpserver import Context

from oh_my_cassette.app import App, ResolvedProject
from oh_my_cassette.cassette.errors import CassetteError

log = logging.getLogger("oh_my_cassette.tools")


def failed(error: CassetteError, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"status": "failed", "error": error.to_dict()}
    body.update(extra)
    return body


def project_fields(app: App, project: ResolvedProject) -> dict[str, Any]:
    return {
        "project_id": project.project_id,
        "editor_url": app.editor_url(project.project_id),
        "version": project.snapshot.version,
    }


def guarded(func: Callable[..., Awaitable[dict[str, Any]]]) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Turn CassetteError into a failed envelope; let everything else surface as a tool error."""

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return await func(*args, **kwargs)
        except CassetteError as exc:
            log.info("%s failed: %s (%s)", func.__name__, exc.code, exc.message)
            return failed(exc)

    return wrapper


class Progress:
    """Adapter from client callbacks to MCP progress notifications with a monotonic counter."""

    def __init__(self, ctx: Context | None) -> None:
        self._ctx = ctx
        self._count = 0

    async def __call__(self, message: str, fraction: float | None = None) -> None:
        self._count += 1
        if self._ctx is None:
            return
        try:
            if fraction is not None:
                await self._ctx.report_progress(max(0.0, min(1.0, fraction)) * 100, 100, message)
            else:
                await self._ctx.report_progress(self._count, None, message)
        except Exception:
            log.debug("progress notification failed", exc_info=True)
