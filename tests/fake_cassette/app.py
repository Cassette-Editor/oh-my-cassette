"""In-process fake of the Cassette-Editor routes the plugin uses.

It is deliberately typed and scripted: tests describe the run they expect (a list of durable events)
and the fake replays them over SSE with sequence ids, enforces control-revision CAS on commands,
serves presigned-style upload targets on its own origin, and walks export jobs through the real
status vocabulary. It is not a simulation of the editing agent.
"""

from __future__ import annotations

import copy
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

PROJECT_ID = "try-session-11111111-2222-4333-8444-555555555555"


def empty_document(project_id: str) -> dict[str, Any]:
    return {
        "projectId": project_id,
        "version": 0,
        "activeSequenceId": "seq_main",
        "settings": {
            "defaultTimebase": {"num": 30, "den": 1},
            "defaultCompositionWidth": 1920,
            "defaultCompositionHeight": 1080,
        },
        "entities": {
            "sequences": {
                "seq_main": {
                    "id": "seq_main",
                    "displayName": "Main Timeline",
                    "kind": "main",
                    "timebase": {"num": 30, "den": 1},
                    "compositionWidth": 1920,
                    "compositionHeight": 1080,
                    "mainTrackId": "v1",
                    "trackIds": ["v1"],
                    "clipIds": [],
                    "transitionIds": [],
                    "semanticAnnotationIds": [],
                }
            },
            "tracks": {
                "v1": {
                    "id": "v1",
                    "sequenceId": "seq_main",
                    "displayName": "V1",
                    "type": "video",
                    "role": "primary",
                    "muted": False,
                    "locked": False,
                    "visible": True,
                    "isMain": True,
                }
            },
            "clips": {},
            "transitions": {},
            "semanticAnnotations": {},
        },
        "order": {"sequenceIds": ["seq_main"]},
    }


def text_clip(clip_id: str, text: str, start: int, duration: int, track: str = "v1") -> dict[str, Any]:
    return {
        "id": clip_id,
        "sequenceId": "seq_main",
        "trackId": track,
        "displayName": text,
        "type": "text",
        "startFrame": start,
        "durationInFrames": duration,
        "connection": {"kind": "free"},
        "source": {"kind": "text", "text": text},
    }


def video_clip(clip_id: str, media_id: str, start: int, duration: int, track: str = "v1") -> dict[str, Any]:
    return {
        "id": clip_id,
        "sequenceId": "seq_main",
        "trackId": track,
        "displayName": clip_id,
        "type": "video",
        "startFrame": start,
        "durationInFrames": duration,
        "connection": {"kind": "free"},
        "source": {"kind": "media-file", "mediaFileId": media_id},
        "sourceWindow": {"startUs": 0, "endUs": duration * 1_000_000 // 30},
    }


@dataclass
class ScriptedRun:
    """Events the fake emits for the next `start` command. `commit_clips` lands document changes."""

    events: list[tuple[str, dict[str, Any]]]
    commit_clips: list[dict[str, Any]] = field(default_factory=list)
    close_stream_early: bool = False  # end the SSE stream before the terminal event (simulates a drop)


@dataclass
class FakeProject:
    project_id: str
    document: dict[str, Any]
    chat_sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    history_cursor: int = 0
    media_files: dict[str, dict[str, Any]] = field(default_factory=dict)
    workspace_revision: int = 0
    owned: bool = False


@dataclass
class FakeSession:
    chat_session_id: str
    project_id: str
    control_revision: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)
    active_run: dict[str, Any] | None = None
    preference: dict[str, Any] = field(
        default_factory=lambda: {
            "revision": 0,
            "mode": "auto",
            "preferredModelId": "openai/gpt-5.6-luna",
            "preferredReasoningEffort": "medium",
            "updatedAt": None,
        }
    )
    pending_after_resume: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


class FakeCassette:
    def __init__(self, *, auth_required: bool = False, token: str = "test-token") -> None:
        self.auth_required = auth_required
        self.token = token
        self.projects: dict[str, FakeProject] = {}
        self.sessions: dict[str, FakeSession] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.scripted: list[ScriptedRun] = []
        self.command_log: list[dict[str, Any]] = []
        self.uploads: dict[str, dict[str, Any]] = {}
        self.objects: dict[str, bytes] = {}
        self.export_jobs: dict[str, dict[str, Any]] = {}
        self.export_manifests: list[dict[str, Any]] = []
        self.export_polls_until_done = 2
        self.fail_first_export: str | None = None  # error text the first export job ends with
        self.readiness_polls_until_ready = 1
        self.readiness_counts: dict[str, int] = {}
        self.reject_video_preparation = True  # mirrors the mediabunny-only literal on current main
        self.force_conflict_once = False
        self.owned_projects_fail = False  # mirrors FK failure under the local bypass

    # ── helpers for tests ──

    def script(self, *runs: ScriptedRun) -> None:
        self.scripted.extend(runs)

    def finish_run(self, run_id: str, events: list[tuple[str, dict[str, Any]]]) -> None:
        """Append events to a run that is still active (re-attach and timeout scenarios)."""
        run = self.runs[run_id]
        session = self.sessions[run["chatSessionId"]]
        self._play(session, run, events, run.get("_scripted"))

    def project(self, project_id: str) -> FakeProject:
        if project_id not in self.projects:
            self.projects[project_id] = FakeProject(
                project_id=project_id, document=empty_document(project_id)
            )
        return self.projects[project_id]

    def seed_project(
        self, project_id: str = PROJECT_ID, clips: list[dict[str, Any]] | None = None
    ) -> FakeProject:
        project = self.project(project_id)
        for clip in clips or []:
            self._land_clip(project, clip)
        return project

    def _land_clip(self, project: FakeProject, clip: dict[str, Any]) -> None:
        doc = project.document
        doc["entities"]["clips"][clip["id"]] = clip
        seq = doc["entities"]["sequences"]["seq_main"]
        if clip["id"] not in seq["clipIds"]:
            seq["clipIds"].append(clip["id"])

    def _commit(
        self,
        project: FakeProject,
        clips: list[dict[str, Any]],
        *,
        label: str,
        run_id: str | None,
        chat_session_id: str | None,
    ) -> int:
        before = project.document["version"]
        for clip in clips:
            self._land_clip(project, clip)
        project.document["version"] = before + 1
        # commits after an undo drop the redo tail
        project.history = project.history[: project.history_cursor]
        project.history.append(
            {
                "groupId": f"grp-{project.document['version']}",
                "cursorSequence": len(project.history) + 1,
                "label": label,
                "actor": {"kind": "agent" if run_id else "user"},
                "source": "agent" if run_id else "manual",
                "versionBefore": before,
                "versionAfter": project.document["version"],
                "committedAt": "2026-09-15T00:00:00.000Z",
                "chatSessionId": chat_session_id,
                "agentRunId": run_id,
                "focusTargets": [],
                "changes": [],
                "summaries": [],
                "_clips": copy.deepcopy(clips),
            }
        )
        project.history_cursor = len(project.history)
        return project.document["version"]

    # ── auth ──

    def _check_auth(self, request: Request) -> Response | None:
        if not self.auth_required:
            return None
        header = request.headers.get("authorization", "")
        if header != f"Bearer {self.token}":
            return JSONResponse(
                {"error": "unauthorized", "message": "Invalid session token"}, status_code=401
            )
        return None

    # ── projects ──

    async def list_projects(self, request: Request) -> Response:
        if (err := self._check_auth(request)) is not None:
            return err
        return JSONResponse(
            {"projects": [{"id": p.project_id, "title": None} for p in self.projects.values() if p.owned]}
        )

    async def create_project(self, request: Request) -> Response:
        if (err := self._check_auth(request)) is not None:
            return err
        if self.owned_projects_fail:
            return JSONResponse(
                {
                    "error": 'insert or update on table "projects" violates foreign key constraint "projects_owner_id_fkey"'
                },
                status_code=503,
            )
        body = await request.json() if await request.body() else {}
        project_id = str(uuid.uuid4())
        project = self.project(project_id)
        project.owned = True
        return JSONResponse(
            {"metadata": {"id": project_id, "slug": project_id, "title": body.get("title")}}, status_code=201
        )

    async def initialize(self, request: Request) -> Response:
        if (err := self._check_auth(request)) is not None:
            return err
        project = self.project(request.path_params["project_id"])
        return JSONResponse(
            {
                "kind": "project_document_snapshot",
                "document": project.document,
                "meta": {
                    "version": project.document["version"],
                    "historyCursorSequence": project.history_cursor,
                },
            }
        )

    async def get_project(self, request: Request) -> Response:
        if (err := self._check_auth(request)) is not None:
            return err
        project = self.projects.get(request.path_params["project_id"])
        if project is None:
            return JSONResponse({"error": "PROJECT_NOT_INITIALIZED"}, status_code=404)
        return JSONResponse(
            {
                "kind": "project_document_snapshot",
                "document": project.document,
                "meta": {
                    "version": project.document["version"],
                    "historyCursorSequence": project.history_cursor,
                },
            }
        )

    async def chat_sessions(self, request: Request) -> Response:
        if (err := self._check_auth(request)) is not None:
            return err
        project = self.project(request.path_params["project_id"])
        if request.method == "GET":
            return JSONResponse({"sessions": list(project.chat_sessions.values())})
        body = await request.json()
        session_id = body.get("id") or str(uuid.uuid4())
        session = {
            "id": session_id,
            "mode": body.get("mode", "chat"),
            "title": body.get("title"),
            "status": "active",
            "canonical_project_id": project.project_id,
        }
        project.chat_sessions[session_id] = session
        self.sessions[session_id] = FakeSession(chat_session_id=session_id, project_id=project.project_id)
        return JSONResponse({"session": session})

    async def edit_results(self, request: Request) -> Response:
        self.project(request.path_params["project_id"])
        run_id_by_msg = {
            r["inputMessageId"]: r
            for r in self.runs.values()
            if r["chatSessionId"] == request.query_params.get("chatSessionId")
        }
        run = run_id_by_msg.get(request.query_params.get("inputMessageId", ""))
        if not run or not run.get("committedEditReceipts"):
            return JSONResponse([])
        versions = [r["documentVersion"] for r in run["committedEditReceipts"]]
        return JSONResponse(
            [
                {
                    "runId": run["runId"],
                    "inputMessageId": run["inputMessageId"],
                    "status": run["status"],
                    "editCount": len(versions),
                    "before": {"revisionId": "rev-before", "documentVersion": min(versions) - 1},
                    "after": {"revisionId": "rev-after", "documentVersion": max(versions)},
                    "aligned": True,
                }
            ]
        )

    # ── history ──

    async def history(self, request: Request) -> Response:
        project = self.projects.get(request.path_params["project_id"])
        if project is None:
            return JSONResponse({"error": "PROJECT_NOT_INITIALIZED"}, status_code=404)
        groups = [{k: v for k, v in g.items() if not k.startswith("_")} for g in project.history]
        return JSONResponse(
            {
                "groups": groups,
                "cursorSequence": project.history_cursor,
                "version": project.document["version"],
                "latestActivity": None,
            }
        )

    def _history_outcome(self, project: FakeProject, command: dict[str, Any]) -> tuple[str, str | None]:
        kind = command.get("kind")
        if kind == "undo":
            return ("ready", None) if project.history_cursor > 0 else ("invalid", "at_start")
        if kind == "redo":
            return ("ready", None) if project.history_cursor < len(project.history) else ("invalid", "at_end")
        if kind == "restore_before":
            if any(g["groupId"] == command.get("groupId") for g in project.history):
                return "ready", None
            return "invalid", "target_not_found"
        return "invalid", "unsupported"

    async def history_preview(self, request: Request) -> Response:
        project = self.project(request.path_params["project_id"])
        body = await request.json()
        status, reason = self._history_outcome(project, body.get("command", {}))
        payload = {
            "status": status,
            "version": project.document["version"],
            "historyCursorSequence": project.history_cursor,
        }
        if reason:
            payload["reason"] = reason
        return JSONResponse(payload, status_code=200 if status == "ready" else 400)

    async def history_commit(self, request: Request) -> Response:
        project = self.project(request.path_params["project_id"])
        if any(s.active_run for s in self.sessions.values() if s.project_id == project.project_id):
            return JSONResponse(
                {
                    "error": "Project timeline is locked by an active agent run.",
                    "code": "project_timeline_locked",
                    "retryable": False,
                },
                status_code=409,
            )
        body = await request.json()
        if (
            body.get("expectedVersion") != project.document["version"]
            or body.get("expectedHistoryCursorSequence") != project.history_cursor
        ):
            return JSONResponse(
                {
                    "status": "stale",
                    "version": project.document["version"],
                    "historyCursorSequence": project.history_cursor,
                },
                status_code=409,
            )
        command = body.get("command", {})
        status, reason = self._history_outcome(project, command)
        if status != "ready":
            return JSONResponse({"status": "invalid", "reason": reason}, status_code=400)
        self._apply_history(project, command)
        return JSONResponse(
            {
                "status": "moved",
                "version": project.document["version"],
                "historyCursorSequence": project.history_cursor,
            }
        )

    def _apply_history(self, project: FakeProject, command: dict[str, Any]) -> None:
        kind = command["kind"]
        if kind == "undo":
            target = project.history_cursor - 1
        elif kind == "redo":
            target = project.history_cursor + 1
        else:
            target = next(i for i, g in enumerate(project.history) if g["groupId"] == command["groupId"])
        # rebuild the document from scratch up to the target cursor
        doc = empty_document(project.project_id)
        for group in project.history[:target]:
            for clip in group["_clips"]:
                doc["entities"]["clips"][clip["id"]] = clip
                if clip["id"] not in doc["entities"]["sequences"]["seq_main"]["clipIds"]:
                    doc["entities"]["sequences"]["seq_main"]["clipIds"].append(clip["id"])
        doc["version"] = project.document["version"] + 1
        project.document = doc
        project.history_cursor = target

    # ── agent ──

    def _state(self, session: FakeSession) -> dict[str, Any]:
        active = None
        if session.active_run:
            run = session.active_run
            active = {
                "runId": run["runId"],
                "taskId": run["taskId"],
                "mode": run["mode"],
                "planLink": None,
                "selectedModelId": run["selectedModelId"],
                "reasoningEffort": run["reasoningEffort"],
                "executionBinding": {
                    "mode": run["mode"],
                    "stage": "conversation",
                    "graphKey": "x",
                    "graphRevision": "1",
                    "stageRevision": 1,
                },
                "status": run["status"],
                "pendingInterruption": run.get("pendingInterruption"),
                "pendingSteering": [],
            }
        return {
            "chatSessionId": session.chat_session_id,
            "projectId": session.project_id,
            "eventCursor": len(session.events),
            "controlRevision": session.control_revision,
            "transcriptWindow": {"messages": [], "limit": 50, "beforeSequence": None, "hasMoreBefore": False},
            "visibleActiveDraft": None,
            "taskLedger": {"taskId": None, "status": "idle", "revision": 0, "items": []},
            "nextRunQueue": {"revision": 0, "item": None},
            "usage": {
                "revision": 0,
                "totals": {},
                "byModel": [],
                "byStage": [],
                "recentTurn": None,
                "context": None,
                "updatedAt": None,
            },
            "executionPreference": session.preference,
            "activeRun": active,
            "projectActiveRuns": [
                {
                    "runId": active["runId"],
                    "chatSessionId": session.chat_session_id,
                    "mode": active["mode"],
                    "status": active["status"],
                    "startedAt": "2026-09-15T00:00:00Z",
                }
            ]
            if active
            else [],
        }

    async def session_state(self, request: Request) -> Response:
        if (err := self._check_auth(request)) is not None:
            return err
        session = self.sessions.get(request.path_params["chat_session_id"])
        if session is None:
            return JSONResponse(
                {
                    "statusCode": 404,
                    "code": "not_found",
                    "error": "Not Found",
                    "message": "chat session not found",
                },
                status_code=404,
            )
        return JSONResponse(self._state(session))

    async def run_state(self, request: Request) -> Response:
        run = self.runs.get(request.path_params["run_id"])
        if run is None:
            return JSONResponse(
                {"statusCode": 404, "code": "not_found", "error": "Not Found", "message": "run not found"},
                status_code=404,
            )
        return JSONResponse({k: v for k, v in run.items() if not k.startswith("_")})

    def _emit(
        self, session: FakeSession, run: dict[str, Any], event_type: str, payload: dict[str, Any]
    ) -> None:
        session.events.append(
            {
                "schemaVersion": 1,
                "sequence": len(session.events) + 1,
                "runId": run["runId"],
                "chatSessionId": session.chat_session_id,
                "projectId": session.project_id,
                "type": event_type,
                "payload": payload,
                "createdAt": "2026-09-15T00:00:00.000Z",
            }
        )

    def _play(
        self,
        session: FakeSession,
        run: dict[str, Any],
        events: list[tuple[str, dict[str, Any]]],
        scripted: ScriptedRun | None,
    ) -> None:
        project = self.project(session.project_id)
        for event_type, payload in events:
            payload = copy.deepcopy(payload)
            if event_type == "project_commit":
                clips = scripted.commit_clips if scripted else []
                version = self._commit(
                    project,
                    clips,
                    label=payload.get("label", "agent edit"),
                    run_id=run["runId"],
                    chat_session_id=session.chat_session_id,
                )
                payload.setdefault("documentVersion", version)
                run["committedEditReceipts"].append({"groupId": f"grp-{version}", "documentVersion": version})
                run["committedEditCount"] = len(run["committedEditReceipts"])
            if event_type == "run_terminal":
                payload.setdefault("committedEditReceipts", run["committedEditReceipts"])
                payload.setdefault("mode", run["mode"])
                payload.setdefault("terminalReason", "final_response")
                payload.setdefault("assistantMessageId", None)
                run["status"] = payload.get("status", "completed")
                run["terminalReason"] = payload.get("terminalReason")
                session.active_run = None
            if event_type == "run_aborted":
                payload.setdefault("committedEditReceipts", run["committedEditReceipts"])
                run["status"] = "aborted"
                session.active_run = None
            if event_type == "run_failed":
                payload.setdefault("committedEditReceipts", run["committedEditReceipts"])
                run["status"] = "failed"
                session.active_run = None
            if event_type == "interrupt_created":
                run["status"] = "interrupt_pending"
                run["pendingInterruption"] = {"kind": payload["kind"], "interruptId": payload["interruptId"]}
            self._emit(session, run, event_type, payload)

    async def commands(self, request: Request) -> Response:
        if (err := self._check_auth(request)) is not None:
            return err
        session = self.sessions.get(request.path_params["chat_session_id"])
        if session is None:
            return JSONResponse(
                {
                    "statusCode": 404,
                    "code": "not_found",
                    "error": "Not Found",
                    "message": "chat session not found",
                },
                status_code=404,
            )
        body = await request.json()
        self.command_log.append(body)
        if self.force_conflict_once:
            self.force_conflict_once = False
            session.control_revision += 1
            return JSONResponse(
                {"state": "conflict", "controlRevision": session.control_revision, "result": None},
                status_code=409,
            )
        if body.get("expectedRevision") != session.control_revision:
            return JSONResponse(
                {"state": "conflict", "controlRevision": session.control_revision, "result": None},
                status_code=409,
            )
        command = body["command"]
        kind = command["type"]
        session.control_revision += 1
        if kind == "start":
            if session.active_run is not None:
                return JSONResponse(
                    {
                        "statusCode": 409,
                        "code": "run_already_active",
                        "error": "Conflict",
                        "message": "A run is already active.",
                    },
                    status_code=409,
                )
            req = command["request"]
            assert req["idempotencyKey"] and req["message"]["content"], (
                "start request must carry idempotencyKey and message"
            )
            run_id, task_id = str(uuid.uuid4()), str(uuid.uuid4())
            run = {
                "runId": run_id,
                "taskId": task_id,
                "chatSessionId": session.chat_session_id,
                "projectId": session.project_id,
                "mode": req["mode"],
                "planLink": None,
                "selectedModelId": req["selectedModelId"],
                "reasoningEffort": req["selectedReasoningEffort"],
                "originalRequest": req["message"]["content"],
                "executionBinding": {},
                "status": "running",
                "terminalReason": None,
                "preRunHistoryCursor": None,
                "modelCallUsage": 0,
                "modelCallLimit": 10,
                "committedEditCount": 0,
                "committedEditReceipts": [],
                "createdAt": "2026-09-15T00:00:00Z",
                "completedAt": None,
                "inputMessageId": req["message"].get("id") or str(uuid.uuid4()),
                "interactionPolicy": req.get("interactionPolicy"),
            }
            self.runs[run_id] = run
            session.active_run = run
            scripted = (
                self.scripted.pop(0)
                if self.scripted
                else ScriptedRun(events=[("run_terminal", {"status": "completed", "finalText": "ok"})])
            )
            events = [
                (
                    "run_started",
                    {
                        "taskId": task_id,
                        "mode": req["mode"],
                        "modelId": req["selectedModelId"],
                        "reasoningEffort": req["selectedReasoningEffort"],
                        "originalRequest": req["message"]["content"],
                        "inputMessageId": run["inputMessageId"],
                        "traceId": "trace",
                        "executionBinding": {},
                        "planLink": None,
                    },
                )
            ]
            events += scripted.events
            run["_scripted"] = scripted
            cursor_at_start = len(session.events) + 1  # sequence run_started will get
            self._play(session, run, events, scripted)
            return JSONResponse(
                {
                    "state": "applied",
                    "controlRevision": session.control_revision,
                    "result": {
                        "runId": run_id,
                        "taskId": task_id,
                        "chatSessionId": session.chat_session_id,
                        "status": "running",
                        "eventCursor": cursor_at_start,
                    },
                }
            )
        if kind == "stop":
            run = self.runs.get(command["runId"])
            if run is None or session.active_run is None:
                return JSONResponse(
                    {
                        "statusCode": 404,
                        "code": "not_found",
                        "error": "Not Found",
                        "message": "run not active",
                    },
                    status_code=404,
                )
            self._play(
                session,
                run,
                [
                    (
                        "run_aborted",
                        {
                            "reason": command.get("reason", "user_stop"),
                            "assistantMessageId": None,
                            "markerMessageId": None,
                        },
                    )
                ],
                None,
            )
            return JSONResponse(
                {"state": "applied", "controlRevision": session.control_revision, "result": {"stopped": True}}
            )
        if kind == "resume":
            run = self.runs.get(command["runId"])
            if run is None or run.get("pendingInterruption") is None:
                return JSONResponse(
                    {
                        "statusCode": 404,
                        "code": "not_found",
                        "error": "Not Found",
                        "message": "no pending interrupt",
                    },
                    status_code=404,
                )
            if run["pendingInterruption"]["interruptId"] != command["interruptId"]:
                return JSONResponse(
                    {
                        "statusCode": 404,
                        "code": "not_found",
                        "error": "Not Found",
                        "message": "unknown interrupt",
                    },
                    status_code=404,
                )
            response = command.get("response") or {}
            scripted: ScriptedRun | None = run.get("_scripted")
            if response.get("action") == "cancel":
                run["pendingInterruption"] = None
                run["status"] = "running"
                run["_answer"] = response
                after = session.pending_after_resume or [
                    ("run_terminal", {"status": "completed", "finalText": "continued without an answer"})
                ]
                session.pending_after_resume = []
                self._play(
                    session,
                    run,
                    [
                        (
                            "interrupt_resolved",
                            {"interruptId": command["interruptId"], "resolution": "declined"},
                        ),
                        ("run_resumed", {}),
                    ]
                    + after,
                    scripted,
                )
                return JSONResponse(
                    {
                        "state": "applied",
                        "controlRevision": session.control_revision,
                        "result": {"ok": True, "replayed": False},
                    }
                )
            if response.get("selectedChoiceId") is None and response.get("freeText") is None:
                return JSONResponse(
                    {
                        "statusCode": 422,
                        "code": "invalid_answer",
                        "error": "Unprocessable",
                        "message": "An answer must carry a selected choice, free text, or both.",
                    },
                    status_code=422,
                )
            question = next(
                (
                    e["payload"]["payload"]
                    for e in reversed(session.events)
                    if e["type"] == "interrupt_created"
                ),
                {},
            )
            if response.get("selectedChoiceId") is not None and response["selectedChoiceId"] not in {
                c["id"] for c in question.get("choices", [])
            }:
                return JSONResponse(
                    {
                        "statusCode": 422,
                        "code": "invalid_answer",
                        "error": "Unprocessable",
                        "message": f'"{response["selectedChoiceId"]}" is not one of the question\'s choice ids.',
                    },
                    status_code=422,
                )
            run["pendingInterruption"] = None
            run["status"] = "running"
            run["_answer"] = response
            after = session.pending_after_resume or [
                ("run_terminal", {"status": "completed", "finalText": f"answered {response}"})
            ]
            session.pending_after_resume = []
            self._play(
                session,
                run,
                [
                    ("interrupt_resolved", {"interruptId": command["interruptId"], "resolution": "answered"}),
                    ("run_resumed", {}),
                ]
                + after,
                scripted,
            )
            return JSONResponse(
                {
                    "state": "applied",
                    "controlRevision": session.control_revision,
                    "result": {"ok": True, "replayed": False},
                }
            )
        if kind == "idle_preference_change":
            session.preference.update(
                {
                    "revision": session.preference["revision"] + 1,
                    "mode": command["mode"],
                    "preferredModelId": command["modelId"],
                    "preferredReasoningEffort": command["reasoningEffort"],
                }
            )
            return JSONResponse(
                {
                    "state": "applied",
                    "controlRevision": session.control_revision,
                    "result": session.preference,
                }
            )
        return JSONResponse(
            {
                "statusCode": 400,
                "code": "bad_request",
                "error": "Bad Request",
                "message": f"unsupported command {kind}",
            },
            status_code=400,
        )

    async def events(self, request: Request) -> Response:
        if (err := self._check_auth(request)) is not None:
            return err
        session = self.sessions.get(request.path_params["chat_session_id"])
        if session is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        after = int(request.query_params.get("after") or request.headers.get("last-event-id") or 0)

        async def body():
            yield b": heartbeat\n\n"
            for event in session.events:
                if event["sequence"] <= after:
                    continue
                run = self.runs.get(event["runId"], {})
                scripted = run.get("_scripted")
                if (
                    scripted
                    and scripted.close_stream_early
                    and event["type"] in ("run_terminal", "run_aborted", "run_failed")
                ):
                    scripted.close_stream_early = False
                    return
                if event["type"] == "assistant_message_started":
                    transient = {
                        "schemaVersion": 1,
                        "replayable": False,
                        "runId": event["runId"],
                        "chatSessionId": session.chat_session_id,
                        "projectId": session.project_id,
                        "type": "assistant_text_delta",
                        "payload": {"text": "…"},
                        "createdAt": event["createdAt"],
                    }
                    yield f"event: agent_transient\ndata: {json.dumps(transient)}\n\n".encode()
                yield f"id: {event['sequence']}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()

        return StreamingResponse(body(), media_type="text/event-stream")

    # ── media ──

    async def workspace(self, request: Request) -> Response:
        project = self.project(request.query_params["sessionId"])
        return JSONResponse(
            {
                "sessionId": project.project_id,
                "revision": project.workspace_revision,
                "files": [],
                "folders": [],
            }
        )

    async def media_files(self, request: Request) -> Response:
        session_id = request.query_params.get("session_id")
        if not session_id:
            return JSONResponse({"error": "session_id is required"}, status_code=400)
        project = self.project(session_id)
        files = [f for f in project.media_files.values() if f["upload_status"] == "completed"]
        return JSONResponse({"files": files})

    async def imports(self, request: Request) -> Response:
        body = await request.json()
        project = self.project(body["sessionId"])
        if body.get("expectedRevision") != project.workspace_revision:
            return JSONResponse({"error": "conflict", "ids": []}, status_code=409)
        accepted, rejected, duplicates = [], [], []
        existing_by_sha = {f["sha256"]: f["id"] for f in project.media_files.values()}
        for item in body["files"]:
            if item["kind"] is None:
                rejected.append({"id": item["id"], "reason": "unsupported_type"})
                continue
            if item["byteLength"] == 0:
                rejected.append({"id": item["id"], "reason": "empty_file"})
                continue
            if item["sha256"] in existing_by_sha:
                duplicates.append({"id": item["id"], "existingMediaId": existing_by_sha[item["sha256"]]})
                continue
            project.media_files[item["id"]] = {
                "id": item["id"],
                "session_id": project.project_id,
                "file_name": item["name"],
                "file_url": "",
                "r2_key": "",
                "upload_status": "pending",
                "analyze_status": None,
                "file_type": item["kind"],
                "metadata": {},
                "description": None,
                "error_message": None,
                "folder_id": None,
                "deleted_at": None,
                "created_at": "",
                "updated_at": "",
                "sha256": item["sha256"],
                "byteLength": item["byteLength"],
                "kind": item["kind"],
            }
            accepted.append(
                {
                    "id": item["id"],
                    "name": item["name"],
                    "kind": item["kind"],
                    "revision": 1,
                    "folderId": None,
                    "workGeneration": 0,
                    "contentId": None,
                    "byteLength": item["byteLength"],
                    "durationUs": None,
                    "capturedAt": None,
                    "uploadStatus": "pending",
                    "publishedAnalysisRevision": None,
                    "importBatchId": body["batchId"],
                }
            )
        project.workspace_revision += 1
        return JSONResponse(
            {
                "request": body,
                "folderMapping": {},
                "folders": [],
                "files": accepted,
                "rejected": rejected,
                "duplicates": duplicates,
                "conflicts": [],
                "reservedBytes": sum(f["byteLength"] for f in accepted),
                "incompleteEmptyDirectoryRoots": [],
            }
        )

    async def upload_register(self, request: Request) -> Response:
        body = await request.json()
        project = self.project(body["sessionId"])
        media = project.media_files.get(body["mediaId"])
        if media is None:
            return JSONResponse({"error": "upload_target_unavailable"}, status_code=409)
        roles = sorted(a["role"] for a in body["artifacts"])
        required = ["canonical", "original", "preview"] if media["kind"] == "video" else ["original"]
        if roles != required or (media["kind"] == "video" and not body.get("preparation")):
            return JSONResponse({"error": "artifact_set_invalid"}, status_code=422)
        if media["kind"] == "video" and self.reject_video_preparation:
            processor = (body.get("preparation") or {}).get("processor", {})
            if processor.get("name") != "mediabunny":
                return JSONResponse(
                    {
                        "error": "invalid_request",
                        "issues": [
                            {
                                "path": ["preparation", "processor", "name"],
                                "message": 'Invalid literal value, expected "mediabunny"',
                            }
                        ],
                    },
                    status_code=400,
                )
        original = next(a for a in body["artifacts"] if a["role"] == "original")
        if original["sha256"] != media["sha256"] or original["byteLength"] != media["byteLength"]:
            return JSONResponse({"error": "local_file_identity_changed"}, status_code=422)
        upload_id = body["uploadId"]
        targets = []
        for artifact in body["artifacts"]:
            object_id = f"{upload_id}-{artifact['role']}"
            targets.append(
                {
                    "role": artifact["role"],
                    "objectId": object_id,
                    "uploadUrl": f"http://fake.storage/objects/{object_id}",
                    "uploadContentType": artifact["mimeType"],
                }
            )
        self.uploads[upload_id] = {
            "id": upload_id,
            "session_id": project.project_id,
            "media_file_id": media["id"],
            "status": "registered",
            "artifacts": body["artifacts"],
            "targets": targets,
            "writable_until": None,
            "error": None,
        }
        media["upload_status"] = "uploading"
        return JSONResponse({"upload": self.uploads[upload_id], "artifacts": targets})

    async def put_object(self, request: Request) -> Response:
        object_id = request.path_params["object_id"]
        self.objects[object_id] = await request.body()
        return Response(status_code=200)

    async def upload_complete(self, request: Request) -> Response:
        body = await request.json()
        upload = self.uploads.get(body["uploadId"])
        if upload is None:
            return JSONResponse({"error": "upload_not_found"}, status_code=404)
        missing = [t["objectId"] for t in upload["targets"] if t["objectId"] not in self.objects]
        if missing:
            return JSONResponse({"error": "artifact_missing", "details": missing}, status_code=422)
        upload["status"] = "queued"
        project = self.project(upload["session_id"])
        project.media_files[upload["media_file_id"]]["upload_status"] = "processing"
        return JSONResponse(upload)

    async def operations_status(self, request: Request) -> Response:
        ids = [i for i in request.query_params.get("ids", "").split(",") if i]
        statuses = []
        for media_id in ids:
            project = next((p for p in self.projects.values() if media_id in p.media_files), None)
            if project is None:
                continue
            media = project.media_files[media_id]
            count = self.readiness_counts.get(media_id, 0) + 1
            self.readiness_counts[media_id] = count
            ready = (
                media["upload_status"] in ("processing", "completed")
                and count >= self.readiness_polls_until_ready
            )
            if ready:
                media["upload_status"] = "completed"
            statuses.append(
                {
                    "mediaFileId": media_id,
                    "terminalState": "active",
                    "fileName": media["file_name"],
                    "uploadStatus": media["upload_status"],
                    "previewStatus": "ready" if ready else "pending",
                    "analyzeStatus": "completed" if ready else "pending",
                    "aiReady": ready,
                    "playbackReady": ready,
                    "fullyReady": ready,
                    "readinessPhase": "ready" if ready else "processing",
                    "errorMessage": None,
                }
            )
        return JSONResponse({"statuses": statuses})

    # ── export ──

    async def create_export_job(self, request: Request) -> Response:
        if (err := self._check_auth(request)) is not None:
            return err
        form = await request.form()
        raw = form.get("manifest")
        if not isinstance(raw, str):
            return JSONResponse({"error": "Missing manifest field"}, status_code=400)
        manifest = json.loads(raw)
        for key in ("projectId", "clips", "tracks", "sequenceTimebase", "outputFileName"):
            if key not in manifest:
                return JSONResponse({"error": f"Invalid manifest: missing {key}"}, status_code=400)
        self.export_manifests.append(manifest)
        job_id = str(uuid.uuid4())
        self.export_jobs[job_id] = {
            "jobId": job_id,
            "status": "queued",
            "step": "Queued",
            "progress": 0,
            "progressPercent": 0,
            "outputFileName": manifest["outputFileName"],
            "error": None,
            "errorCode": None,
            "cancelRequested": False,
            "fileUrl": None,
            "lambdaOutKey": None,
            "statusUrl": f"/api/export/jobs/{job_id}",
            "_polls": 0,
        }
        return JSONResponse(
            {k: v for k, v in self.export_jobs[job_id].items() if not k.startswith("_")}, status_code=202
        )

    async def export_job(self, request: Request) -> Response:
        job = self.export_jobs.get(request.path_params["job_id"])
        if job is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        job["_polls"] += 1
        if job["status"] not in ("done", "error", "cancelled"):
            if self.fail_first_export and job is next(iter(self.export_jobs.values())):
                job.update({"status": "error", "step": "Rendering", "error": self.fail_first_export})
            elif job["_polls"] >= self.export_polls_until_done:
                job.update(
                    {
                        "status": "done",
                        "step": "Done",
                        "progress": 1,
                        "progressPercent": 100,
                        "fileUrl": f"/api/export/jobs/{job['jobId']}/file",
                    }
                )
            else:
                job.update(
                    {"status": "rendering", "step": "Rendering", "progress": 0.5, "progressPercent": 50}
                )
        return JSONResponse({k: v for k, v in job.items() if not k.startswith("_")})

    async def export_file(self, request: Request) -> Response:
        job = self.export_jobs.get(request.path_params["job_id"])
        if job is None or job["status"] != "done":
            return JSONResponse({"error": "not_ready"}, status_code=409)
        self.objects["export.mp4"] = b"\x00\x00\x00\x18ftypmp42" + b"x" * 1024
        return RedirectResponse(url="http://fake.storage/objects/export.mp4", status_code=302)

    async def get_object(self, request: Request) -> Response:
        data = self.objects.get(request.path_params["object_id"])
        if data is None:
            return PlainTextResponse("missing", status_code=404)
        return Response(content=data, media_type="application/octet-stream")

    async def cancel_export(self, request: Request) -> Response:
        job = self.export_jobs.get(request.path_params["job_id"])
        if job is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        job.update({"status": "cancelled", "cancelRequested": True})
        return JSONResponse({k: v for k, v in job.items() if not k.startswith("_")})

    async def verify(self, request: Request) -> Response:
        body = await request.json()
        if body.get("email") == "user@example.com" and body.get("password") == "correct-horse-battery":
            return JSONResponse(
                {
                    "user": {"id": "u1", "email": body["email"]},
                    "session": {"access_token": self.token, "refresh_token": "r", "expires_in": 3600},
                    "isFullUser": True,
                }
            )
        return JSONResponse({"error": "invalid_credentials"}, status_code=401)


def make_app(fake: FakeCassette) -> Starlette:
    return Starlette(
        routes=[
            Route("/api/agent-auth/verify", fake.verify, methods=["POST"]),
            Route("/api/projects", fake.list_projects, methods=["GET"]),
            Route("/api/projects", fake.create_project, methods=["POST"]),
            Route("/api/projects/{project_id}", fake.get_project, methods=["GET"]),
            Route("/api/projects/{project_id}/initialize", fake.initialize, methods=["POST"]),
            Route("/api/projects/{project_id}/chat-sessions", fake.chat_sessions, methods=["GET", "POST"]),
            Route("/api/projects/{project_id}/edit-results", fake.edit_results, methods=["GET"]),
            Route("/api/projects/{project_id}/timeline-history", fake.history, methods=["GET"]),
            Route(
                "/api/projects/{project_id}/timeline-history-command/preview",
                fake.history_preview,
                methods=["POST"],
            ),
            Route(
                "/api/projects/{project_id}/timeline-history-command/commit",
                fake.history_commit,
                methods=["POST"],
            ),
            Route("/api/agent/sessions/{chat_session_id}/state", fake.session_state, methods=["GET"]),
            Route("/api/agent/sessions/{chat_session_id}/commands", fake.commands, methods=["POST"]),
            Route("/api/agent/sessions/{chat_session_id}/events", fake.events, methods=["GET"]),
            Route("/api/agent/runs/{run_id}", fake.run_state, methods=["GET"]),
            Route("/api/media/workspace", fake.workspace, methods=["GET"]),
            Route("/api/media/files", fake.media_files, methods=["GET"]),
            Route("/api/media/imports", fake.imports, methods=["POST"]),
            Route("/api/media/imports/upload", fake.upload_register, methods=["POST"]),
            Route("/api/media/imports/upload/complete", fake.upload_complete, methods=["POST"]),
            Route("/api/media/operations/status", fake.operations_status, methods=["GET"]),
            Route("/objects/{object_id}", fake.put_object, methods=["PUT"]),
            Route("/objects/{object_id}", fake.get_object, methods=["GET"]),
            Route("/api/export/jobs", fake.create_export_job, methods=["POST"]),
            Route("/api/export/jobs/{job_id}", fake.export_job, methods=["GET"]),
            Route("/api/export/jobs/{job_id}/file", fake.export_file, methods=["GET"]),
            Route("/api/export/jobs/{job_id}/cancel", fake.cancel_export, methods=["POST"]),
        ]
    )
