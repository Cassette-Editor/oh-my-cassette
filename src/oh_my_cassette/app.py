"""Composition root: settings + local state + backend clients, plus project resolution shared by tools."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from oh_my_cassette.cassette.agent import AgentClient
from oh_my_cassette.cassette.auth import TokenSource
from oh_my_cassette.cassette.errors import CassetteError
from oh_my_cassette.cassette.export import ExportClient
from oh_my_cassette.cassette.http import CassetteHttp
from oh_my_cassette.cassette.media import MediaClient
from oh_my_cassette.cassette.models import ProjectSnapshot
from oh_my_cassette.cassette.prepare import MediaTools
from oh_my_cassette.cassette.projects import ProjectsClient, editor_url, new_demo_project_id
from oh_my_cassette.settings import Settings
from oh_my_cassette.state import ProjectRecord, StateStore


@dataclass
class ResolvedProject:
    record: ProjectRecord
    snapshot: ProjectSnapshot

    @property
    def project_id(self) -> str:
        return self.record.project_id

    @property
    def chat_session_id(self) -> str:
        return self.record.chat_session_id

    @property
    def document(self) -> dict[str, Any]:
        return self.snapshot.document


class App:
    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None, cwd: str | None = None
    ) -> None:
        self.settings = settings
        self.cwd = os.path.abspath(cwd or os.getcwd())
        self.state = StateStore(settings.home)
        self.http = CassetteHttp(
            settings.api_url,
            token_provider=TokenSource(settings),
            timeout_sec=settings.http_timeout_sec,
            transport=transport,
        )
        self.tools = MediaTools(settings.ffmpeg, settings.ffprobe)
        self.projects = ProjectsClient(self.http)
        self.agent = AgentClient(self.http)
        self.media = MediaClient(self.http, self.tools)
        self.export = ExportClient(self.http)

    async def aclose(self) -> None:
        await self.http.aclose()

    # ── project lifecycle ──

    def editor_url(self, project_id: str) -> str:
        return editor_url(self.settings.web_url, project_id)

    async def create_project(
        self, title: str | None, *, cwd: str | None = None, mode: str = "auto"
    ) -> ResolvedProject:
        if self.settings.anonymous:
            project_id = new_demo_project_id()
            snapshot = await self.projects.initialize(project_id)
        else:
            metadata = await self.projects.create_owned(title)
            project_id = metadata.id
            snapshot = await self.projects.initialize(project_id)
        session = await self.projects.create_chat_session(project_id, mode, title)
        record = ProjectRecord(
            project_id=project_id, chat_session_id=session.id, title=title, anonymous=self.settings.anonymous
        )
        self.state.put_project(record)
        self.state.bind_cwd(cwd or self.cwd, project_id)
        return ResolvedProject(record=record, snapshot=snapshot)

    async def open_project(
        self, project_id: str, *, cwd: str | None = None, mode: str = "auto"
    ) -> ResolvedProject:
        snapshot = await self.projects.ensure_initialized(project_id)
        record = self.state.get_project(project_id)
        if record is None:
            sessions = await self.projects.list_chat_sessions(project_id)
            session = sessions[0] if sessions else await self.projects.create_chat_session(project_id, mode)
            record = ProjectRecord(
                project_id=project_id, chat_session_id=session.id, anonymous=self.settings.anonymous
            )
            self.state.put_project(record)
        else:
            self.state.update_project(project_id)
        self.state.bind_cwd(cwd or self.cwd, project_id)
        return ResolvedProject(record=record, snapshot=snapshot)

    async def resolve(self, project_id: str | None, *, cwd: str | None = None) -> ResolvedProject:
        """Find the project a tool call refers to: explicit id → cwd binding → most recent."""
        if project_id:
            return await self.open_project(project_id, cwd=cwd)
        record = self.state.project_for_cwd(cwd or self.cwd)
        if record is None:
            recent = self.state.list_projects()
            record = recent[0] if recent else None
        if record is None:
            raise CassetteError(
                "no_project",
                "no Cassette project is bound to this workspace yet. Call cassette_project with action=create "
                "(or action=open with a project_id).",
            )
        snapshot = await self.projects.ensure_initialized(record.project_id)
        self.state.update_project(record.project_id)
        return ResolvedProject(record=record, snapshot=snapshot)

    async def refresh_snapshot(self, project_id: str) -> ProjectSnapshot:
        return await self.projects.ensure_initialized(project_id)

    async def media_names(self, project_id: str) -> dict[str, str]:
        try:
            files = await self.media.list_files(project_id)
        except CassetteError:
            return {}
        return {f.id: f.file_name for f in files}

    def local_media(self, project_id: str) -> dict[str, str]:
        record = self.state.get_project(project_id)
        return dict(record.media) if record else {}

    def artifacts_dir(self, project_id: str) -> Path:
        path = self.settings.home / "artifacts" / project_id
        path.mkdir(parents=True, exist_ok=True)
        return path
