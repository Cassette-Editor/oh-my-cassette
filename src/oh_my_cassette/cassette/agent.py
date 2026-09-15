"""Agent runtime client: session commands, state, durable event stream and the run driver."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from oh_my_cassette.cassette.errors import CassetteError, HttpError, TransportError
from oh_my_cassette.cassette.http import CassetteHttp
from oh_my_cassette.cassette.models import (
    DURABLE_EVENT_TYPES,
    TERMINAL_EVENT_TYPES,
    AskUserQuestion,
    CommandResponse,
    EditorSnapshot,
    EditReceipt,
    RunEvent,
    RunMessage,
    RunRecord,
    SessionState,
    StartAgentRunRequest,
    StartRunResult,
    TransientEvent,
)

log = logging.getLogger("oh_my_cassette.agent")

ProgressCallback = Callable[[str, float | None], Awaitable[None]]

RunOutcomeStatus = Literal["completed", "not_done", "aborted", "failed", "needs_input", "running", "timeout"]

TOOL_PROGRESS_LABELS = {
    "timeline_edit": "editing the timeline",
    "read_resource": "reading project resources",
    "ask_user": "asking a question",
}


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class RunOutcome:
    status: RunOutcomeStatus
    run_id: str
    task_id: str | None = None
    input_message_id: str | None = None
    mode: str | None = None
    final_text: str | None = None
    terminal_reason: str | None = None
    receipts: list[EditReceipt] = field(default_factory=list)
    error: dict[str, Any] | None = None
    question: AskUserQuestion | None = None
    interrupt_id: str | None = None
    interrupt_kind: str | None = None
    budget: dict[str, Any] | None = None
    last_cursor: int = 0
    tool_calls: list[str] = field(default_factory=list)
    commits: list[int] = field(default_factory=list)
    events_seen: int = 0

    @property
    def committed_versions(self) -> list[int]:
        versions = sorted({r.documentVersion for r in self.receipts} | set(self.commits))
        return versions


class AgentClient:
    def __init__(self, http: CassetteHttp) -> None:
        self._http = http

    # ── state & commands ──

    async def state(self, chat_session_id: str, project_id: str) -> SessionState:
        body = await self._http.get_json(
            f"/api/agent/sessions/{chat_session_id}/state", params={"projectId": project_id}
        )
        return SessionState.model_validate(body)

    async def run(self, run_id: str) -> RunRecord:
        return RunRecord.model_validate(await self._http.get_json(f"/api/agent/runs/{run_id}"))

    async def command(
        self,
        chat_session_id: str,
        command: dict[str, Any],
        *,
        expected_revision: int,
        retry_on_conflict: bool = True,
    ) -> CommandResponse:
        body = {"commandId": str(uuid.uuid4()), "expectedRevision": expected_revision, "command": command}
        response = await self._http.request(
            "POST",
            f"/api/agent/sessions/{chat_session_id}/commands",
            json_body=body,
            ok=(200, 409),
            retries=0,
        )
        try:
            parsed = CommandResponse.model_validate(response.json())
        except Exception as exc:  # 409 bodies that are plain errors
            if response.status_code == 409:
                raise HttpError(409, "conflict", response.text[:300], retryable=True) from exc
            raise
        if response.status_code == 409 or parsed.state == "conflict":
            if not retry_on_conflict:
                raise HttpError(
                    409, "conflict", "session control revision moved", details=parsed.model_dump()
                )
            log.debug("control revision conflict, retrying at %s", parsed.controlRevision)
            return await self.command(
                chat_session_id, command, expected_revision=parsed.controlRevision, retry_on_conflict=False
            )
        return parsed

    def build_start_request(
        self,
        *,
        project_id: str,
        chat_session_id: str,
        mode: str,
        message: str,
        model_id: str,
        reasoning_effort: str,
        locale: str,
        unattended: bool,
        active_sequence_id: str,
        total_frames: int,
        timebase: dict[str, int],
        turn_no: int,
    ) -> StartAgentRunRequest:
        digest = hashlib.sha256(f"{project_id}|{chat_session_id}|{turn_no}|{message}".encode()).hexdigest()
        return StartAgentRunRequest(
            projectId=project_id,
            chatSessionId=chat_session_id,
            mediaSessionId=project_id,
            mode=mode,  # type: ignore[arg-type]
            interactionPolicy="non_interactive" if (unattended and mode == "auto") else None,
            selectedModelId=model_id,
            selectedReasoningEffort=reasoning_effort,  # type: ignore[arg-type]
            locale=locale,
            message=RunMessage(id=str(uuid.uuid4()), content=message, createdAt=_now_iso()),
            editorSnapshot=EditorSnapshot(
                activeSequenceId=active_sequence_id,
                totalFrames=total_frames,
                fps=timebase.get("num", 30) / max(1, timebase.get("den", 1)),
                sequenceTimebase=timebase,
            ),
            idempotencyKey=f"omc-{digest[:32]}",
        )

    async def start(
        self, chat_session_id: str, request: StartAgentRunRequest, *, expected_revision: int
    ) -> StartRunResult:
        payload = request.model_dump(exclude_none=True)
        response = await self.command(
            chat_session_id, {"type": "start", "request": payload}, expected_revision=expected_revision
        )
        if not isinstance(response.result, dict):
            raise CassetteError(
                "start_failed", "start command returned no run", details=response.model_dump()
            )
        return StartRunResult.model_validate(response.result)

    async def stop(self, chat_session_id: str, run_id: str, *, expected_revision: int) -> CommandResponse:
        return await self.command(
            chat_session_id,
            {"type": "stop", "runId": run_id, "reason": "user_stop"},
            expected_revision=expected_revision,
        )

    async def resume(
        self,
        chat_session_id: str,
        run_id: str,
        interrupt_id: str,
        *,
        expected_revision: int,
        selected_choice_id: str | None,
        free_text: str | None,
    ) -> CommandResponse:
        answer = {"selectedChoiceId": selected_choice_id, "freeText": free_text}
        return await self.command(
            chat_session_id,
            {"type": "resume", "runId": run_id, "interruptId": interrupt_id, "response": answer},
            expected_revision=expected_revision,
        )

    async def decline_interrupt(
        self, chat_session_id: str, run_id: str, interrupt_id: str, *, expected_revision: int
    ) -> CommandResponse:
        return await self.command(
            chat_session_id,
            {
                "type": "resume",
                "runId": run_id,
                "interruptId": interrupt_id,
                "response": {"action": "cancel"},
            },
            expected_revision=expected_revision,
        )

    async def set_preference(
        self,
        chat_session_id: str,
        *,
        project_id: str,
        expected_preference_revision: int,
        expected_revision: int,
        mode: str,
        model_id: str,
        reasoning_effort: str,
    ) -> CommandResponse:
        return await self.command(
            chat_session_id,
            {
                "type": "idle_preference_change",
                "projectId": project_id,
                "expectedPreferenceRevision": expected_preference_revision,
                "mode": mode,
                "modelId": model_id,
                "reasoningEffort": reasoning_effort,
            },
            expected_revision=expected_revision,
        )

    # ── events ──

    async def events(
        self, chat_session_id: str, project_id: str, *, after: int, read_timeout: float = 90
    ) -> AsyncIterator[RunEvent | TransientEvent]:
        async for frame in self._http.sse(
            f"/api/agent/sessions/{chat_session_id}/events",
            params={"after": after, "projectId": project_id},
            last_event_id=str(after) if after else None,
            read_timeout=read_timeout,
        ):
            try:
                data = frame.json()
            except ValueError:
                continue
            if frame.id is not None and frame.event in DURABLE_EVENT_TYPES:
                yield RunEvent.model_validate(data)
            elif frame.event == "agent_transient":
                yield TransientEvent.model_validate(data)

    async def follow_run(
        self,
        chat_session_id: str,
        project_id: str,
        run_id: str,
        *,
        after: int,
        deadline: float,
        on_progress: ProgressCallback | None = None,
        heartbeat_sec: float = 45,
    ) -> RunOutcome:
        """Consume events until the run reaches a terminal or interrupt state, or the deadline.

        Reconnects transparently on transport drops (the stream is durable server-side). Progress is
        reported on every durable event and at least every ``heartbeat_sec`` so hosts with idle
        timeouts keep the call alive.
        """
        outcome = RunOutcome(status="running", run_id=run_id, last_cursor=after)
        assistant_text: list[str] = []
        last_report = time.monotonic()

        async def report(message: str) -> None:
            nonlocal last_report
            last_report = time.monotonic()
            if on_progress is not None:
                try:
                    await on_progress(message, None)
                except Exception:  # progress must never break the run
                    log.debug("progress callback failed", exc_info=True)

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                stream = self.events(
                    chat_session_id,
                    project_id,
                    after=outcome.last_cursor,
                    read_timeout=min(heartbeat_sec + 15, max(5, remaining)),
                )
                async for event in stream:
                    if isinstance(event, TransientEvent):
                        if event.runId == run_id and event.type == "assistant_text_delta":
                            text = (event.payload or {}).get("text")
                            if isinstance(text, str):
                                assistant_text.append(text)
                        if time.monotonic() - last_report >= heartbeat_sec:
                            await report("agent is working…")
                        continue
                    outcome.last_cursor = max(outcome.last_cursor, event.sequence)
                    if event.runId != run_id:
                        continue
                    outcome.events_seen += 1
                    finished = await self._apply_event(outcome, event, report)
                    if finished:
                        if outcome.final_text is None and assistant_text:
                            outcome.final_text = "".join(assistant_text)
                        return outcome
                    if time.monotonic() >= deadline:
                        break
                # stream ended without a terminal event: re-check the run row before reconnecting
                record = await self._safe_run(run_id)
                if record is not None and record.status in ("completed", "not_done", "aborted", "failed"):
                    return self._outcome_from_record(outcome, record, "".join(assistant_text) or None)
            except TransportError as exc:
                log.warning("event stream dropped (%s); reconnecting", exc)
                await asyncio.sleep(1.0)
            except HttpError as exc:
                if exc.status in (404, 401, 403):
                    raise
                await asyncio.sleep(1.5)
            if time.monotonic() - last_report >= heartbeat_sec:
                await report("waiting for the agent…")
        outcome.status = "timeout"
        if outcome.final_text is None and assistant_text:
            outcome.final_text = "".join(assistant_text)
        return outcome

    async def _safe_run(self, run_id: str) -> RunRecord | None:
        try:
            return await self.run(run_id)
        except CassetteError:
            return None

    @staticmethod
    def _outcome_from_record(outcome: RunOutcome, record: RunRecord, text: str | None) -> RunOutcome:
        outcome.status = record.status  # type: ignore[assignment]
        outcome.terminal_reason = record.terminalReason
        outcome.receipts = record.committedEditReceipts
        outcome.task_id = outcome.task_id or record.taskId
        outcome.mode = outcome.mode or record.mode
        if outcome.final_text is None:
            outcome.final_text = text
        return outcome

    async def _apply_event(
        self, outcome: RunOutcome, event: RunEvent, report: Callable[[str], Awaitable[None]]
    ) -> bool:
        payload = event.payload or {}
        kind = event.type
        if kind == "run_started":
            outcome.task_id = payload.get("taskId")
            outcome.input_message_id = payload.get("inputMessageId")
            outcome.mode = payload.get("mode")
            await report(f"run started ({payload.get('mode')}, {payload.get('modelId')})")
        elif kind == "tool_call_started":
            name = str(payload.get("toolName") or "tool")
            outcome.tool_calls.append(name)
            await report(TOOL_PROGRESS_LABELS.get(name, f"running {name}"))
        elif kind == "project_commit":
            version = payload.get("documentVersion") or payload.get("versionAfter") or payload.get("version")
            if isinstance(version, int):
                outcome.commits.append(version)
                await report(f"timeline committed (v{version})")
            else:
                await report("timeline committed")
        elif kind == "task_snapshot":
            items = payload.get("items") or []
            done = sum(
                1 for item in items if isinstance(item, dict) and item.get("status") in ("done", "completed")
            )
            if items:
                await report(f"plan progress {done}/{len(items)}")
        elif kind == "interrupt_created":
            outcome.status = "needs_input"
            outcome.interrupt_id = payload.get("interruptId")
            outcome.interrupt_kind = payload.get("kind")
            question = payload.get("payload") or {}
            if payload.get("kind") == "ask_user" and isinstance(question, dict):
                outcome.question = AskUserQuestion.model_validate(question)
            await report("the agent needs an answer")
            return True
        elif kind == "run_paused":
            await report("run paused")
        elif kind == "run_terminal":
            outcome.status = payload.get("status") or "completed"
            outcome.final_text = payload.get("finalText")
            outcome.terminal_reason = payload.get("terminalReason")
            outcome.budget = payload.get("budget")
            outcome.receipts = [
                EditReceipt.model_validate(r) for r in payload.get("committedEditReceipts") or []
            ]
            await report(f"run {outcome.status}")
            return True
        elif kind == "run_aborted":
            outcome.status = "aborted"
            outcome.terminal_reason = payload.get("reason")
            outcome.receipts = [
                EditReceipt.model_validate(r) for r in payload.get("committedEditReceipts") or []
            ]
            await report("run aborted")
            return True
        elif kind == "run_failed":
            outcome.status = "failed"
            outcome.error = {
                "code": payload.get("errorCode") or "run_failed",
                "message": payload.get("message") or "the run failed",
                "retryable": bool(payload.get("retryable")),
            }
            outcome.receipts = [
                EditReceipt.model_validate(r) for r in payload.get("committedEditReceipts") or []
            ]
            await report("run failed")
            return True
        return False


def outcome_is_terminal(status: str) -> bool:
    return status in {"completed", "not_done", "aborted", "failed"}


__all__ = ["AgentClient", "RunOutcome", "outcome_is_terminal", "TERMINAL_EVENT_TYPES"]
