from __future__ import annotations

import time
from typing import Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations

from oh_my_cassette.app import App, ResolvedProject
from oh_my_cassette.cassette.agent import RunOutcome
from oh_my_cassette.cassette.errors import CassetteError, HttpError
from oh_my_cassette.cassette.models import (
    DEFAULT_MODEL_ID,
    DEFAULT_REASONING_EFFORT,
    MODEL_IDS,
    REASONING_EFFORTS,
    SessionState,
)
from oh_my_cassette.render.digest import (
    active_sequence,
    sequence_arrays,
    timeline_delta,
    timeline_digest,
    total_frames,
)
from oh_my_cassette.tools.common import Progress, guarded, project_fields

RUN_NEXT = {
    "completed": "relay final_text and the timeline delta to the user; continue the conversation with the next cassette_run.",
    "not_done": "the agent stopped before satisfying the request; show final_text, then ask the user how to proceed.",
    "needs_input": "ask the user the question, then call cassette_answer with choice_id or free_text.",
    "aborted": "the run was stopped; committed edits stay. Continue with a new cassette_run.",
    "failed": "report error to the user; retry only if error.retryable is true.",
    "timeout": "the run is still going server-side; call cassette_status to re-attach (do not restart the request).",
    "running": "call cassette_status to keep following the run.",
}


async def finish_outcome(
    app: App, project: ResolvedProject, outcome: RunOutcome, before: dict[str, Any]
) -> dict[str, Any]:
    """Turn a run outcome into the tool envelope: delta, digest, question, error."""
    after = (await app.refresh_snapshot(project.project_id)).document
    names = await app.media_names(project.project_id)
    delta = timeline_delta(before, after)
    body: dict[str, Any] = {
        "status": outcome.status,
        "project_id": project.project_id,
        "editor_url": app.editor_url(project.project_id),
        "run_id": outcome.run_id,
        "mode": outcome.mode,
        "final_text": outcome.final_text,
        "terminal_reason": outcome.terminal_reason,
        "version_from": delta.version_from,
        "version_to": delta.version_to,
        "committed_versions": outcome.committed_versions,
        "timeline_delta": delta.summary(before, after),
        "timeline": timeline_digest(after, names),
        "tool_calls": outcome.tool_calls,
        "next": RUN_NEXT.get(outcome.status, ""),
    }
    if outcome.budget:
        body["budget"] = outcome.budget
    if outcome.error:
        body["error"] = outcome.error
    if outcome.status == "needs_input":
        body["interrupt_id"] = outcome.interrupt_id
        body["interrupt_kind"] = outcome.interrupt_kind
        if outcome.question:
            body["question"] = {
                "question": outcome.question.question,
                "reason": outcome.question.reason,
                "choices": [{"id": c.id, "label": c.label} for c in outcome.question.choices],
                "allows_free_text": outcome.question.allowsFreeText,
            }
    app.state.update_project(
        project.project_id,
        event_cursor=outcome.last_cursor,
        last_run_id=outcome.run_id,
        last_input_message_id=outcome.input_message_id or project.record.last_input_message_id,
    )
    return body


# How long cassette_status waits for the durable tail of a run that ended while detached.
DETACHED_REPLAY_SEC = 10.0


def _mark_detached_delta(body: dict[str, Any], outcome: RunOutcome) -> None:
    """The snapshot read before a detached replay already contains the run's commits."""
    versions = outcome.committed_versions
    if versions and body["version_from"] == body["version_to"]:
        body["version_from"] = versions[0] - 1
        body["timeline_delta"] = (
            f"v{versions[0] - 1}→v{body['version_to']}: {len(versions)} commit(s) landed while no client "
            "was attached; see timeline"
        )


def _snapshot_hints(document: dict[str, Any]) -> tuple[str, int, dict[str, int]]:
    seq = active_sequence(document) or {}
    timebase = (
        seq.get("timebase") or document.get("settings", {}).get("defaultTimebase") or {"num": 30, "den": 1}
    )
    return (
        str(document.get("activeSequenceId", "seq_main")),
        total_frames(sequence_arrays(document)["clips"]),
        dict(timebase),
    )


def _pick_model(state: SessionState, app: App, model: str | None, effort: str | None) -> tuple[str, str]:
    pref = state.executionPreference
    model_id = model or app.settings.model_id or (pref.preferredModelId if pref else None) or DEFAULT_MODEL_ID
    if model_id in ("luna", "terra", "sol"):
        model_id = f"openai/gpt-5.6-{model_id}"
    if model_id not in MODEL_IDS:
        raise CassetteError("bad_request", f"unknown model {model_id!r}; use one of {', '.join(MODEL_IDS)}")
    effort_id = (
        effort
        or app.settings.reasoning_effort
        or (pref.preferredReasoningEffort if pref else None)
        or DEFAULT_REASONING_EFFORT
    )
    if effort_id not in REASONING_EFFORTS:
        raise CassetteError(
            "bad_request",
            f"unknown reasoning effort {effort_id!r}; use one of {', '.join(REASONING_EFFORTS)}",
        )
    return model_id, effort_id


def register(server: MCPServer, app: App) -> None:
    @server.tool(
        name="cassette_run",
        title="Send one editing turn to the Cassette agent",
        description=(
            "Send the user's request to the Cassette editing agent as one conversational turn and wait for it. "
            "Pass the user's words verbatim in `message`; do not rewrite or expand them. mode=auto (default) lets "
            "the agent edit the timeline; mode=chat only answers questions. The call streams progress and returns "
            "a typed status: completed, not_done, needs_input (then call cassette_answer), aborted, failed, or "
            "timeout (then call cassette_status). The reply carries final_text, the version delta "
            "(version_from→version_to), a bounded timeline digest and editor_url. If a run is already active on "
            "the project the call attaches to it instead of starting another."
        ),
        annotations=ToolAnnotations(title="Run editing turn", read_only_hint=False, open_world_hint=True),
    )
    @guarded
    async def cassette_run(
        message: str,
        ctx: Context,
        project_id: str | None = None,
        mode: Literal["auto", "chat"] = "auto",
        unattended: bool = False,
        model: str | None = None,
        effort: str | None = None,
        wait_sec: int | None = None,
    ) -> dict[str, Any]:
        if not message.strip():
            raise CassetteError("bad_request", "message must not be empty")
        project = await app.resolve(project_id)
        before = project.document
        progress = Progress(ctx)
        state = await app.agent.state(project.chat_session_id, project.project_id)
        budget = float(wait_sec if wait_sec else app.settings.run_timeout_sec)
        deadline = time.monotonic() + budget
        if state.activeRun is not None:
            active = state.activeRun
            if active.status == "interrupt_pending":
                outcome = RunOutcome(
                    status="needs_input",
                    run_id=active.runId,
                    task_id=active.taskId,
                    mode=active.mode,
                    interrupt_id=active.pendingInterruption.interruptId
                    if active.pendingInterruption
                    else None,
                    interrupt_kind=active.pendingInterruption.kind if active.pendingInterruption else None,
                    last_cursor=state.eventCursor,
                )
                body = await finish_outcome(app, project, outcome, before)
                body["note"] = (
                    "a previous run is waiting for an answer; answer it with cassette_answer (or cassette_stop) before sending a new request."
                )
                return body
            await progress(f"attaching to the active run {active.runId[:8]}…")
            outcome = await app.agent.follow_run(
                project.chat_session_id,
                project.project_id,
                active.runId,
                after=project.record.event_cursor,
                deadline=deadline,
                on_progress=progress,
            )
            body = await finish_outcome(app, project, outcome, before)
            body["note"] = (
                "a run was already active; this call followed it. Re-send the new request with cassette_run."
            )
            return body

        model_id, effort_id = _pick_model(state, app, model, effort)
        sequence_id, frames, timebase = _snapshot_hints(before)
        request = app.agent.build_start_request(
            project_id=project.project_id,
            chat_session_id=project.chat_session_id,
            mode=mode,
            message=message,
            model_id=model_id,
            reasoning_effort=effort_id,
            locale=app.settings.locale,
            unattended=unattended,
            active_sequence_id=sequence_id,
            total_frames=frames,
            timebase=timebase,
            turn_no=state.eventCursor,
        )
        await progress("starting run")
        try:
            started = await app.agent.start(
                project.chat_session_id, request, expected_revision=state.controlRevision
            )
        except HttpError as exc:
            if exc.code == "run_already_active":
                raise CassetteError(
                    "run_already_active",
                    "a run is already active on this project; call cassette_status to follow it",
                    retryable=True,
                ) from exc
            raise
        app.state.update_project(
            project.project_id, last_run_id=started.runId, last_input_message_id=request.message.id
        )
        outcome = await app.agent.follow_run(
            project.chat_session_id,
            project.project_id,
            started.runId,
            after=max(0, started.eventCursor - 1),
            deadline=deadline,
            on_progress=progress,
        )
        outcome.input_message_id = outcome.input_message_id or request.message.id
        return await finish_outcome(app, project, outcome, before)

    @server.tool(
        name="cassette_answer",
        title="Answer the agent's pending question",
        description=(
            "Resume a run that returned status=needs_input. Pass choice_id for one of the offered choices, or "
            "free_text when the question allows it. decline=true answers nothing and lets the agent continue on its "
            "own judgment (use cassette_stop to abort instead). Waits for the run to continue and returns the same "
            "envelope as cassette_run."
        ),
        annotations=ToolAnnotations(title="Answer question", read_only_hint=False, open_world_hint=True),
    )
    @guarded
    async def cassette_answer(
        ctx: Context,
        project_id: str | None = None,
        choice_id: str | None = None,
        free_text: str | None = None,
        decline: bool = False,
        wait_sec: int | None = None,
    ) -> dict[str, Any]:
        project = await app.resolve(project_id)
        before = project.document
        state = await app.agent.state(project.chat_session_id, project.project_id)
        active = state.activeRun
        if active is None or active.status != "interrupt_pending" or active.pendingInterruption is None:
            raise CassetteError("no_pending_question", "no run is waiting for an answer on this project")
        interrupt_id = active.pendingInterruption.interruptId
        if not interrupt_id:
            raise CassetteError(
                "no_pending_question",
                f"pending interruption of kind {active.pendingInterruption.kind} cannot be answered here",
            )
        if decline:
            await app.agent.decline_interrupt(
                project.chat_session_id, active.runId, interrupt_id, expected_revision=state.controlRevision
            )
        else:
            if choice_id is None and (free_text is None or not free_text.strip()):
                raise CassetteError("bad_request", "provide choice_id or free_text (or decline=true)")
            try:
                await app.agent.resume(
                    project.chat_session_id,
                    active.runId,
                    interrupt_id,
                    expected_revision=state.controlRevision,
                    selected_choice_id=choice_id,
                    free_text=free_text.strip() if free_text else None,
                )
            except HttpError as exc:
                if exc.status in (400, 422):
                    raise CassetteError("invalid_answer", exc.message, details=exc.details) from exc
                raise
        budget = float(wait_sec if wait_sec else app.settings.run_timeout_sec)
        outcome = await app.agent.follow_run(
            project.chat_session_id,
            project.project_id,
            active.runId,
            after=project.record.event_cursor,
            deadline=time.monotonic() + budget,
            on_progress=Progress(ctx),
        )
        return await finish_outcome(app, project, outcome, before)

    @server.tool(
        name="cassette_status",
        title="Re-attach to the current run",
        description=(
            "Report the project's run state and, when a run is active, follow it for up to wait_sec seconds "
            "(default: follow until it ends, bounded by the server timeout). Use it after a host timeout or when "
            "cassette_run returned status=timeout/running. With wait_sec=0 it just returns the state."
        ),
        annotations=ToolAnnotations(title="Run status", read_only_hint=True, open_world_hint=True),
    )
    @guarded
    async def cassette_status(
        ctx: Context, project_id: str | None = None, wait_sec: int | None = None
    ) -> dict[str, Any]:
        project = await app.resolve(project_id)
        before = project.document
        state = await app.agent.state(project.chat_session_id, project.project_id)
        base = {
            **project_fields(app, project),
            "chat_session_id": project.chat_session_id,
            "event_cursor": state.eventCursor,
            "last_run_id": project.record.last_run_id,
            "preference": state.executionPreference.model_dump() if state.executionPreference else None,
        }
        if state.activeRun is None:
            last_run_id = project.record.last_run_id
            if last_run_id and state.eventCursor > project.record.event_cursor:
                # The last run finished while no client was following it: replay its durable tail
                # so the host gets the real outcome (final text, terminal reason, commits).
                outcome = await app.agent.follow_run(
                    project.chat_session_id,
                    project.project_id,
                    last_run_id,
                    after=project.record.event_cursor,
                    deadline=time.monotonic() + DETACHED_REPLAY_SEC,
                    on_progress=Progress(ctx),
                )
                if outcome.status not in ("running", "timeout"):
                    body = await finish_outcome(app, project, outcome, before)
                    _mark_detached_delta(body, outcome)
                    body.update(base)
                    return body
            record = None
            if last_run_id:
                try:
                    record = await app.agent.run(last_run_id)
                except CassetteError:
                    record = None
            return {
                "status": "idle",
                **base,
                "last_run": record.model_dump() if record else None,
                "timeline": timeline_digest(before, await app.media_names(project.project_id)),
                "next": "no run is active; send the next request with cassette_run.",
            }
        active = state.activeRun
        if wait_sec == 0 or active.status == "interrupt_pending":
            outcome = RunOutcome(
                status="needs_input" if active.status == "interrupt_pending" else "running",
                run_id=active.runId,
                task_id=active.taskId,
                mode=active.mode,
                last_cursor=project.record.event_cursor,
                interrupt_id=active.pendingInterruption.interruptId if active.pendingInterruption else None,
                interrupt_kind=active.pendingInterruption.kind if active.pendingInterruption else None,
            )
            body = await finish_outcome(app, project, outcome, before)
            body.update(base)
            return body
        budget = float(wait_sec if wait_sec else app.settings.run_timeout_sec)
        outcome = await app.agent.follow_run(
            project.chat_session_id,
            project.project_id,
            active.runId,
            after=project.record.event_cursor,
            deadline=time.monotonic() + budget,
            on_progress=Progress(ctx),
        )
        body = await finish_outcome(app, project, outcome, before)
        body.update(base)
        return body

    @server.tool(
        name="cassette_stop",
        title="Stop the active run",
        description="Stop the run currently active on the project. Edits it already committed are kept (never rolled back).",
        annotations=ToolAnnotations(
            title="Stop run", read_only_hint=False, destructive_hint=False, open_world_hint=True
        ),
    )
    @guarded
    async def cassette_stop(ctx: Context, project_id: str | None = None) -> dict[str, Any]:
        project = await app.resolve(project_id)
        before = project.document
        state = await app.agent.state(project.chat_session_id, project.project_id)
        if state.activeRun is None:
            return {"status": "idle", **project_fields(app, project), "note": "no run was active"}
        active = state.activeRun
        await app.agent.stop(project.chat_session_id, active.runId, expected_revision=state.controlRevision)
        outcome = await app.agent.follow_run(
            project.chat_session_id,
            project.project_id,
            active.runId,
            after=project.record.event_cursor,
            deadline=time.monotonic() + 120,
            on_progress=Progress(ctx),
        )
        return await finish_outcome(app, project, outcome, before)
