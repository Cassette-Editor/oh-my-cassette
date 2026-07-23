"""Local stdio MCP server for Codex and Claude.

Protocol messages are written only by the MCP SDK on stdout.  All human-readable
diagnostics go to stderr.
"""

import asyncio
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Literal, Sequence
from urllib.parse import unquote, urlparse

from mcp import types
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field, ValidationError

import runtime_config

from .models import (
    AnswerQuestionInput,
    SessionPhase,
    CancelJobInput,
    IngestMediaInput,
    JamendoMatcherInput,
    JobStatusInput,
    EditInput,
    ListAssetsInput,
    TimelineInput,
    MakePromptInput,
    MatchBgmInput,
    MatchExactBgmInput,
    ReviewCompletionInput,
    RunJobInput,
    ToolEnvelope,
)
from .runtime import LocalMcpRuntime


@dataclass
class McpLifespanContext:
    runtime: LocalMcpRuntime


@asynccontextmanager
async def lifespan(_: FastMCP) -> AsyncIterator[McpLifespanContext]:
    errors = runtime_config.configure_mcp_process_environment()
    runtime = LocalMcpRuntime(errors)
    if errors:
        print(
            "oh-my-cassette: local configuration requires attention; tools will return structured details",
            file=sys.stderr,
            flush=True,
        )
    yield McpLifespanContext(runtime=runtime)


class ArtifactFastMCP(FastMCP[McpLifespanContext]):
    """Append validated artifact ResourceLink blocks to structured tool output."""

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        try:
            result = await super().call_tool(name, arguments)
        except ToolError as exc:
            cause = exc.__cause__
            if not isinstance(cause, ValidationError):
                raise
            context = self.get_context()
            runtime = _runtime(context)
            session_id = str(arguments.get("session_id") or "").strip() or None
            job_id = str(arguments.get("job_id") or "").strip() or None
            envelope = runtime._failure(
                "validation_error",
                "Tool arguments did not match the declared MCP schema.",
                details={
                    "issues": cause.errors(
                        include_url=False,
                        include_context=False,
                        include_input=False,
                    )
                },
                session_id=session_id,
                job_id=job_id,
            )
            tool = self._tool_manager.get_tool(name)
            if tool is None:
                raise
            result = tool.fn_metadata.convert_result(envelope)
        if not (isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict)):
            return result
        content, structured = result
        blocks = list(content) if isinstance(content, Sequence) else []
        for artifact in structured.get("artifacts") or []:
            if not isinstance(artifact, dict):
                continue
            blocks.append(
                types.ResourceLink(
                    type="resource_link",
                    name=str(artifact.get("name") or "Cassette export"),
                    uri=str(artifact.get("resource_uri") or artifact.get("uri") or ""),
                    description="Validated Cassette export artifact",
                    mimeType=str(artifact.get("mime_type") or "application/octet-stream"),
                    size=int(artifact.get("size") or 0),
                )
            )
        return blocks, structured


mcp = ArtifactFastMCP(
    "cassette",
    instructions=(
        "Local video-editing MCP runtime for Oh My Cassette. It uses stdio, opens no port, "
        "and connects directly to the separate Cassette backend. "
        "Guided flow: call cassette_ingest_media once per source file (reuse the returned session_id), "
        "confirm assets with cassette_list_assets, optionally match BGM, build the brief with "
        "cassette_make_prompt, then start cassette_run_job (background by default). "
        "Poll cassette_job_status with wait_for_change_sec=30 and route on the typed phase and next_action "
        "fields, never on prose: needs_user means ask the user then call cassette_answer_question; "
        "review_required means evaluate the result and call cassette_review_completion (only "
        "decision=export renders); exported or succeeded means present the validated artifacts; "
        "failed, cancelled, or timed_out means report the structured error. Do not tight-poll. "
        "Ground every statement about project state in cassette_timeline, never in memory, and "
        "name the version in replies. Small named edits (trim, text, delete, undo) go through "
        "cassette_edit when CASSETTE_DIRECT_EDIT=1: read the timeline first, pass "
        "expected_version, and on stale_timeline re-read and retry; creative or multi-step "
        "briefs go through cassette_run_job. Job envelopes carry editor_url — a live view of "
        "the real editor (timeline + preview, zero render): hand it to the user once at job "
        "start and again at questions/review, offering to open it locally; do not repeat it on "
        "every poll. Status envelopes carry timeline_delta (what changed) and plan_progress — "
        "relay them instead of re-describing state. A needs_user question with reason "
        "edit_plan_review is the edit plan itself: relay it with the link and answer via "
        "cassette_answer_question with approve, revise <feedback>, or reject; if the resume "
        "returns resume_not_waiting_for_user the user already decided in the editor tab — "
        "re-check status. "
        "If a tool returns auth_required, show error.details.setup_command as a private terminal "
        "command; never collect credentials in chat."
    ),
    lifespan=lifespan,
    log_level="WARNING",
)


class ElicitedAnswer(BaseModel):
    """Schema for answering a pending Cassette question via MCP elicitation."""

    response: str = Field(description="The user's answer to the pending Cassette question.")


def _pending_question(envelope: Any) -> str:
    data = envelope.data if isinstance(envelope.data, dict) else {}
    job = data.get("job") if isinstance(data.get("job"), dict) else {}
    questions = job.get("questions") if isinstance(job.get("questions"), list) else []
    for entry in reversed(questions):
        if isinstance(entry, dict):
            text = str(entry.get("question") or "").strip()
            if text:
                return text
    return ""


async def _maybe_elicit_needs_user(ctx: Context, envelope: Any) -> Any:
    """Collect a needs_user answer through MCP elicitation when the client supports it.

    Anything short of an accepted, non-empty response leaves the envelope
    untouched so hosts without elicitation keep the documented tool round-trip.
    """
    try:
        if getattr(envelope, "phase", None) != SessionPhase.NEEDS_USER or not getattr(envelope, "job_id", None):
            return envelope
        capabilities = getattr(getattr(ctx.session, "client_params", None), "capabilities", None)
        if getattr(capabilities, "elicitation", None) is None:
            return envelope
        question = _pending_question(envelope)
        if not question:
            return envelope
        result = await ctx.elicit(message=question, schema=ElicitedAnswer)
        if getattr(result, "action", "") != "accept" or getattr(result, "data", None) is None:
            return envelope
        response = str(result.data.response or "").strip()
        if not response:
            return envelope
        return await _run_sync(
            _runtime(ctx).answer_question,
            {"job_id": envelope.job_id, "response": response},
        )
    except Exception:
        return envelope


def _runtime(context: Context) -> LocalMcpRuntime:
    return context.request_context.lifespan_context.runtime


async def _client_roots(context: Context) -> list[Path]:
    roots: list[Path] = []
    try:
        result = await context.session.list_roots()
    except Exception:  # client root support is optional
        result = None
    for item in getattr(result, "roots", []) or []:
        parsed = urlparse(str(getattr(item, "uri", "")))
        if parsed.scheme != "file":
            continue
        candidate = Path(unquote(parsed.path)).expanduser().resolve()
        roots.append(candidate)
    return roots


async def _run_sync(function, *args):
    return await asyncio.to_thread(function, *args)


@mcp.tool(
    description=(
        "Ingest a trusted local media file from the active host project or an explicitly configured "
        "media root. Generates a cryptographically random session_id when omitted."
    ),
    structured_output=True,
)
async def cassette_ingest_media(
    source_path: str,
    ctx: Context,
    original_name: str | None = None,
    media_type: Literal["video", "image", "audio", "file", "unknown"] | None = None,
    chat_id: str | None = None,
    user_id: str | None = None,
    message_id: str | None = None,
    chat_type: str | None = None,
    thread_id: str | None = None,
    platform: str | None = None,
    caption: str | None = None,
    session_id: str | None = None,
) -> ToolEnvelope:
    request = IngestMediaInput.model_validate(
        {
            "source_path": source_path,
            "original_name": original_name,
            "media_type": media_type,
            "chat_id": chat_id,
            "user_id": user_id,
            "message_id": message_id,
            "chat_type": chat_type,
            "thread_id": thread_id,
            "platform": platform,
            "caption": caption,
            "session_id": session_id,
        }
    )
    roots = await _client_roots(ctx)
    return await _run_sync(_runtime(ctx).ingest_media, request.model_dump(exclude_none=True), roots)


@mcp.tool(description="List media assets isolated to one Cassette session.", structured_output=True)
async def cassette_list_assets(
    ctx: Context,
    session_id: str | None = None,
    chat_id: str | None = None,
) -> ToolEnvelope:
    request = ListAssetsInput(session_id=session_id, chat_id=chat_id)
    return await _run_sync(_runtime(ctx).list_assets, request.model_dump(exclude_none=True))


@mcp.tool(
    description=(
        "Read the live Cassette timeline as a bounded text digest (CTL). Call this before any "
        "statement about project state — never answer from memory. contact_sheet=true also tiles "
        "the stored clip posters into one image (zero render)."
    ),
    structured_output=True,
)
async def cassette_timeline(
    session_id: str,
    ctx: Context,
    detail: str | None = None,
    profile: Literal["aligned", "gateway"] | None = None,
    contact_sheet: bool = False,
) -> ToolEnvelope:
    request = TimelineInput(session_id=session_id, detail=detail, profile=profile, contact_sheet=contact_sheet)
    return await _run_sync(_runtime(ctx).timeline, request.model_dump(exclude_none=True))


@mcp.tool(
    description=(
        "Surgical no-LLM timeline edit through the manual-editor command lane (requires "
        "CASSETTE_DIRECT_EDIT=1). Use for small named changes (trim, text, delete, undo) after "
        "reading cassette_timeline; big or creative briefs go through cassette_run_job. input is "
        'always {"payload": {...}}. Pass '
        "expected_version from the last timeline read; tool_name 'undo' with "
        "input.cursorSequence rewinds the shared operation history."
    ),
    structured_output=True,
)
async def cassette_edit(
    session_id: str,
    tool_name: str,
    ctx: Context,
    input: dict[str, Any] | None = None,
    expected_version: int | None = None,
) -> ToolEnvelope:
    request = EditInput(session_id=session_id, tool_name=tool_name, input=input, expected_version=expected_version)
    return await _run_sync(_runtime(ctx).edit, request.model_dump(exclude_none=True))


@mcp.tool(
    description="Build a complete Cassette edit prompt from a natural-language instruction and session assets.",
    structured_output=True,
)
async def cassette_make_prompt(
    instruction: str,
    ctx: Context,
    session_id: str | None = None,
    chat_id: str | None = None,
    requires_assets: bool = True,
    output_format: str | None = None,
    duration: str | None = None,
    style: str | None = None,
    cassette_language: Literal["zh", "en"] | None = None,
    language: Literal["zh", "en"] | None = None,
    constraints: dict[str, Any] | None = None,
) -> ToolEnvelope:
    request = MakePromptInput.model_validate(
        {
            "instruction": instruction,
            "session_id": session_id,
            "chat_id": chat_id,
            "requires_assets": requires_assets,
            "output_format": output_format,
            "duration": duration,
            "style": style,
            "cassette_language": cassette_language,
            "language": language,
            "constraints": constraints or {},
        }
    )
    return await _run_sync(_runtime(ctx).make_prompt, request.model_dump(exclude_none=True))


@mcp.tool(
    description="Match and optionally register a Free To Use background-music asset for a session.",
    structured_output=True,
)
async def cassette_match_bgm(
    session_id: str,
    instruction: str,
    search_queries: list[str],
    ctx: Context,
    optimization_enabled: bool = False,
    continue_after_match: bool = True,
    fallback_from: str | None = None,
    fallback_reason: str | None = None,
) -> ToolEnvelope:
    request = MatchBgmInput(
        session_id=session_id,
        instruction=instruction,
        search_queries=search_queries,
        optimization_enabled=optimization_enabled,
        continue_after_match=continue_after_match,
        fallback_from=fallback_from,
        fallback_reason=fallback_reason,
    )
    return await _run_sync(
        _runtime(ctx).simple_session_tool,
        "cassette_match_bgm",
        request.model_dump(exclude_none=True),
    )


@mcp.tool(
    description="Match an exact song and artist, optionally download it, and register it with the session.",
    structured_output=True,
)
async def cassette_match_exact_bgm(
    session_id: str,
    instruction: str,
    title: str,
    ctx: Context,
    songTitle: str | None = None,
    song_title: str | None = None,
    artist: str | None = None,
    singer: str | None = None,
    optimization_enabled: bool = False,
    continue_after_match: bool = True,
    download: bool = True,
) -> ToolEnvelope:
    request = MatchExactBgmInput(
        session_id=session_id,
        instruction=instruction,
        title=title,
        songTitle=songTitle,
        song_title=song_title,
        artist=artist,
        singer=singer,
        optimization_enabled=optimization_enabled,
        continue_after_match=continue_after_match,
        download=download,
    )
    return await _run_sync(
        _runtime(ctx).simple_session_tool,
        "cassette_match_exact_bgm",
        request.model_dump(exclude_none=True),
    )


@mcp.tool(
    description="Search Jamendo with validated fixed-form music preferences and optionally register a result.",
    structured_output=True,
)
async def jamendo_music_matcher(
    userQuery: str,
    searchTerms: list[str],
    ctx: Context,
    user_query: str | None = None,
    search_terms: list[str] | None = None,
    fuzzyTags: list[str] | None = None,
    fuzzy_tags: list[str] | None = None,
    excludeTerms: list[str] | None = None,
    exclude_terms: list[str] | None = None,
    vocalInstrumental: Literal["vocal", "instrumental"] | None = None,
    vocalinstrumental: Literal["vocal", "instrumental"] | None = None,
    searchPlan: dict[str, Any] | str | None = None,
    search_plan: dict[str, Any] | str | None = None,
    repairJson: dict[str, Any] | str | None = None,
    download: bool = True,
    seed: int | None = None,
    limit: int | None = None,
    limitOverride: int | None = None,
    outputDir: str | None = None,
    session_id: str | None = None,
) -> ToolEnvelope:
    request = JamendoMatcherInput.model_validate(
        {
            "userQuery": userQuery,
            "user_query": user_query,
            "searchTerms": searchTerms,
            "search_terms": search_terms,
            "fuzzyTags": fuzzyTags,
            "fuzzy_tags": fuzzy_tags,
            "excludeTerms": excludeTerms,
            "exclude_terms": exclude_terms,
            "vocalInstrumental": vocalInstrumental,
            "vocalinstrumental": vocalinstrumental,
            "searchPlan": searchPlan,
            "search_plan": search_plan,
            "repairJson": repairJson,
            "download": download,
            "seed": seed,
            "limit": limit,
            "limitOverride": limitOverride,
            "outputDir": outputDir,
            "session_id": session_id,
        }
    )
    return await _run_sync(
        _runtime(ctx).simple_session_tool,
        "jamendo_music_matcher",
        request.model_dump(exclude_none=True),
    )


@mcp.tool(
    description=(
        "Classify a Cassette question using question mode, or resume an interrupted job using validated "
        "job_id and response fields."
    ),
    structured_output=True,
)
async def cassette_answer_question(
    ctx: Context,
    question: str | None = None,
    instruction: str | None = None,
    asset_count: int | None = None,
    context: dict[str, Any] | None = None,
    job_id: str | None = None,
    response: str | None = None,
) -> ToolEnvelope:
    request = AnswerQuestionInput.model_validate(
        {
            "question": question,
            "instruction": instruction,
            "asset_count": asset_count,
            "context": context or {},
            "job_id": job_id,
            "response": response,
        }
    )
    return await _run_sync(_runtime(ctx).answer_question, request.model_dump(exclude_none=True))


@mcp.tool(
    description=(
        "Start a Cassette edit. Local MCP execution is background-by-default; set wait=true only for "
        "compatibility when a blocking call is intentional."
    ),
    structured_output=True,
)
async def cassette_run_job(
    ctx: Context,
    message: str | None = None,
    export: bool | None = None,
    prompt: str | None = None,
    chat_message: str | None = None,
    cassette_message: str | None = None,
    instruction: str | None = None,
    session_id: str | None = None,
    chat_id: str | None = None,
    url: str | None = None,
    wait: bool = False,
    timeout_sec: int | None = None,
    selectors: dict[str, Any] | None = None,
    cassette_model: str | None = None,
    model: str | None = None,
    thinking_level: str | None = None,
    cassette_language: Literal["zh", "en"] | None = None,
    language: Literal["zh", "en"] | None = None,
) -> ToolEnvelope:
    request = RunJobInput.model_validate(
        {
            "message": message,
            "export": export,
            "prompt": prompt,
            "chat_message": chat_message,
            "cassette_message": cassette_message,
            "instruction": instruction,
            "session_id": session_id,
            "chat_id": chat_id,
            "url": url,
            "wait": wait,
            "timeout_sec": timeout_sec,
            "selectors": selectors or {},
            "cassette_model": cassette_model,
            "model": model,
            "thinking_level": thinking_level,
            "cassette_language": cassette_language,
            "language": language,
        }
    )
    return await _run_sync(_runtime(ctx).run_job, request.model_dump(exclude_none=True))


@mcp.tool(
    description=(
        "Read one job or recent session jobs. wait_for_change_sec performs a bounded long-poll from 0 to 30 seconds."
    ),
    structured_output=True,
)
async def cassette_job_status(
    ctx: Context,
    job_id: str | None = None,
    session_id: str | None = None,
    limit: int = 10,
    wait_for_change_sec: float = 0.0,
) -> ToolEnvelope:
    request = JobStatusInput(
        job_id=job_id,
        session_id=session_id,
        limit=limit,
        wait_for_change_sec=wait_for_change_sec,
    )
    loop = asyncio.get_running_loop()

    def _tick(elapsed: float, total: float, stage: str) -> None:
        # report_progress is a no-op unless the client sent a progressToken.
        asyncio.run_coroutine_threadsafe(ctx.report_progress(round(elapsed, 1), total, stage or None), loop)

    envelope = await _run_sync(_runtime(ctx).job_status, request.model_dump(exclude_none=True), _tick)
    return await _maybe_elicit_needs_user(ctx, envelope)


@mcp.tool(
    description=(
        "Resolve a review-required completion. Rendering starts only for an explicit, validated decision=export."
    ),
    structured_output=True,
)
async def cassette_review_completion(
    job_id: str,
    decision: Literal["export", "continue", "needs_user", "failed"],
    reason: str,
    ctx: Context,
    summary: str | None = None,
) -> ToolEnvelope:
    request = ReviewCompletionInput(
        job_id=job_id,
        decision=decision,
        reason=reason,
        summary=summary,
    )
    return await _run_sync(_runtime(ctx).review_completion, request.model_dump(exclude_none=True))


@mcp.tool(description="Request cooperative cancellation of a persisted Cassette job.", structured_output=True)
async def cassette_cancel_job(job_id: str, ctx: Context) -> ToolEnvelope:
    request = CancelJobInput(job_id=job_id)
    return await _run_sync(_runtime(ctx).cancel_job, request.model_dump(exclude_none=True))


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
