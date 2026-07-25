from __future__ import annotations


class CassetteError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None, recoverable: bool = True):
        super().__init__(message)
        self.code = code
        self.details = details or {}
        self.recoverable = recoverable
