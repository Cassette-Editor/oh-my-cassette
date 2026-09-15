"""Project, chat-session, document, edit-result and timeline-history calls."""

from __future__ import annotations

import uuid
from typing import Any

from oh_my_cassette.cassette.errors import HttpError
from oh_my_cassette.cassette.http import CassetteHttp, error_from_response
from oh_my_cassette.cassette.models import (
    ChatSession,
    EditResult,
    ProjectMetadata,
    ProjectSnapshot,
    TimelineHistory,
    TimelineHistoryOutcome,
)

DEMO_PREFIX = "try-session-"


def new_demo_project_id() -> str:
    return f"{DEMO_PREFIX}{uuid.uuid4()}"


def is_demo_project_id(project_id: str) -> bool:
    return project_id.startswith(("try-session-", "agent-session-", "root-session-", "cmyn_test-session-"))


def editor_url(web_url: str, project_id: str) -> str:
    if is_demo_project_id(project_id):
        session_id = project_id.split("-session-", 1)[1]
        return f"{web_url}/try?projectSessionId={session_id}"
    return f"{web_url}/editor/p/{project_id}"


class ProjectsClient:
    def __init__(self, http: CassetteHttp) -> None:
        self._http = http

    # ── ownership & identity ──

    async def create_owned(self, title: str | None) -> ProjectMetadata:
        body = await self._http.post_json("/api/projects", {"title": title} if title else {}, ok=(200, 201))
        return ProjectMetadata.model_validate(body["metadata"])

    async def list_owned(self) -> list[ProjectMetadata]:
        body = await self._http.get_json("/api/projects")
        return [ProjectMetadata.model_validate(item) for item in body.get("projects", [])]

    async def initialize(self, project_id: str) -> ProjectSnapshot:
        body = await self._http.post_json(f"/api/projects/{project_id}/initialize", {})
        return ProjectSnapshot.model_validate(body)

    async def snapshot(self, project_id: str) -> ProjectSnapshot | None:
        try:
            body = await self._http.get_json(f"/api/projects/{project_id}")
        except HttpError as exc:
            if exc.status == 404:
                return None
            raise
        return ProjectSnapshot.model_validate(body)

    async def ensure_initialized(self, project_id: str) -> ProjectSnapshot:
        snapshot = await self.snapshot(project_id)
        if snapshot is not None:
            return snapshot
        return await self.initialize(project_id)

    # ── chat sessions ──

    async def create_chat_session(self, project_id: str, mode: str, title: str | None = None) -> ChatSession:
        payload: dict[str, Any] = {"id": str(uuid.uuid4()), "mode": mode}
        if title:
            payload["title"] = title
        body = await self._http.post_json(f"/api/projects/{project_id}/chat-sessions", payload)
        return ChatSession.model_validate(body["session"])

    async def list_chat_sessions(self, project_id: str) -> list[ChatSession]:
        body = await self._http.get_json(f"/api/projects/{project_id}/chat-sessions")
        return [ChatSession.model_validate(item) for item in body.get("sessions", [])]

    # ── edit results ──

    async def edit_results(
        self, project_id: str, chat_session_id: str, input_message_id: str
    ) -> list[EditResult]:
        body = await self._http.get_json(
            f"/api/projects/{project_id}/edit-results",
            params={"chatSessionId": chat_session_id, "inputMessageId": input_message_id},
        )
        return [EditResult.model_validate(item) for item in body]

    # ── history ──

    async def history(self, project_id: str) -> TimelineHistory:
        body = await self._http.get_json(f"/api/projects/{project_id}/timeline-history")
        return TimelineHistory.model_validate(body)

    async def history_preview(self, project_id: str, command: dict[str, Any]) -> TimelineHistoryOutcome:
        response = await self._http.request(
            "POST",
            f"/api/projects/{project_id}/timeline-history-command/preview",
            json_body={"command": command},
            ok=(200, 400),
        )
        return TimelineHistoryOutcome.model_validate(response.json())

    async def history_commit(
        self, project_id: str, command: dict[str, Any], *, expected_version: int, expected_cursor: int
    ) -> TimelineHistoryOutcome:
        response = await self._http.request(
            "POST",
            f"/api/projects/{project_id}/timeline-history-command/commit",
            json_body={
                "commandId": str(uuid.uuid4()),
                "command": command,
                "expectedVersion": expected_version,
                "expectedHistoryCursorSequence": expected_cursor,
            },
            ok=(200, 400, 409),
        )
        data = response.json()
        if not isinstance(data, dict) or "status" not in data:
            raise error_from_response(response)
        return TimelineHistoryOutcome.model_validate(data)
