"""Typed failures raised by the Cassette client.

Every tool maps these to a ``status="failed"`` envelope with the same ``code`` so hosts route on a
stable vocabulary instead of prose.
"""

from __future__ import annotations

from typing import Any


class CassetteError(Exception):
    """A failure with a stable machine code, an HTTP status when one exists, and a retry hint."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.status is not None:
            out["http_status"] = self.status
        if self.details is not None:
            out["details"] = self.details
        return out


class TransportError(CassetteError):
    """The backend could not be reached at all (DNS, refused connection, timeout)."""

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__("backend_unreachable", message, retryable=True, details=details)


class HttpError(CassetteError):
    """A non-2xx response. ``code`` comes from the response body when the backend sent one."""

    def __init__(
        self, status: int, code: str, message: str, *, retryable: bool = False, details: Any = None
    ) -> None:
        super().__init__(code, message, status=status, retryable=retryable, details=details)


class ToolNeedsFfmpeg(CassetteError):
    def __init__(self, binary: str) -> None:
        super().__init__(
            "ffmpeg_missing",
            f"`{binary}` was not found on PATH. Install ffmpeg (https://ffmpeg.org/download.html) and retry.",
        )
