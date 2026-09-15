"""Durable local state: one JSON file under ``OH_MY_CASSETTE_HOME`` (default ``~/.oh-my-cassette``).

The backend is the source of truth for everything about a project. This file only remembers what the
backend cannot: which project a working directory uses, which chat session the plugin drives, the
local path behind each imported media id (for contact sheets), the last consumed event cursor, and
the last run per project so ``cassette_status`` can re-attach after a host timeout.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

STATE_VERSION = 1


@dataclass
class ProjectRecord:
    project_id: str
    chat_session_id: str
    title: str | None = None
    anonymous: bool = True
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    event_cursor: int = 0
    last_run_id: str | None = None
    last_input_message_id: str | None = None
    media: dict[str, str] = field(default_factory=dict)  # media_file_id -> local path
    imports: dict[str, str] = field(default_factory=dict)  # sha256 -> media_file_id


class StateStore:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.path = home / "state.json"
        self._lock = threading.RLock()
        self._data: dict[str, Any] | None = None

    # ── persistence ──

    def _load(self) -> dict[str, Any]:
        if self._data is not None:
            return self._data
        data: dict[str, Any] = {"version": STATE_VERSION, "projects": {}, "cwd": {}}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text("utf-8"))
                if isinstance(loaded, dict) and loaded.get("version") == STATE_VERSION:
                    data = loaded
            except (OSError, ValueError):
                pass
        data.setdefault("projects", {})
        data.setdefault("cwd", {})
        self._data = data
        return data

    def _save(self) -> None:
        data = self._load()
        self.home.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.home, 0o700)
        except OSError:
            pass
        fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=self.home)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ── projects ──

    def get_project(self, project_id: str) -> ProjectRecord | None:
        with self._lock:
            raw = self._load()["projects"].get(project_id)
            return ProjectRecord(**raw) if raw else None

    def put_project(self, record: ProjectRecord) -> None:
        with self._lock:
            self._load()["projects"][record.project_id] = asdict(record)
            self._save()

    def update_project(self, project_id: str, **changes: Any) -> ProjectRecord:
        with self._lock:
            record = self.get_project(project_id)
            if record is None:
                raise KeyError(project_id)
            for key, value in changes.items():
                setattr(record, key, value)
            record.last_used_at = time.time()
            self.put_project(record)
            return record

    def list_projects(self) -> list[ProjectRecord]:
        with self._lock:
            records = [ProjectRecord(**raw) for raw in self._load()["projects"].values()]
            records.sort(key=lambda r: r.last_used_at, reverse=True)
            return records

    def forget_project(self, project_id: str) -> None:
        with self._lock:
            data = self._load()
            data["projects"].pop(project_id, None)
            data["cwd"] = {cwd: pid for cwd, pid in data["cwd"].items() if pid != project_id}
            self._save()

    # ── cwd binding ──

    def project_for_cwd(self, cwd: str) -> ProjectRecord | None:
        with self._lock:
            project_id = self._load()["cwd"].get(os.path.abspath(cwd))
            return self.get_project(project_id) if project_id else None

    def bind_cwd(self, cwd: str, project_id: str) -> None:
        with self._lock:
            self._load()["cwd"][os.path.abspath(cwd)] = project_id
            self._save()
