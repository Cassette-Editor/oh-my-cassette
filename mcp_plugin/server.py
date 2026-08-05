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
from mcp.server.fastmcp.resources import FileResource
from pydantic import BaseModel, Field, ValidationError

import runtime_config

from .models import (
    AnswerQuestionInput,
    SessionPhase,
    CancelJobInput,
    ConfigInput,
    IngestMediaInput,
    JamendoMatcherInput,
    JobStatusInput,
    EditInput,
    ListAssetsInput,
    LoginInput,
    TimelineInput,
    MakePromptInput,
    MatchBgmInput,
    MatchExactBgmInput,
    ReviewCompletionInput,
    RunJobInput,
    ToolEnvelope,
)
from .runtime import LocalMcpRuntime
from . import update_check


# A client that advertised roots should answer roots/list promptly; this bound only exists so
# that a host which advertises the capability and then stalls degrades to "no roots" instead of
# wedging the ingest call behind it.
_ROOTS_TIMEOUT_SEC = 5.0


@dataclass
class McpLifespanContext:
    runtime: LocalMcpRuntime
    client_profile_logged: bool = False


def _startup_auth_notice() -> str:
    """One line on whether this machine held Cassette credentials when the server started.

    Read at import rather than in the lifespan: `instructions` is captured for the initialize
    reply before the lifespan body runs, so appending there is a silent no-op (verified against
    a real stdio client). Import is also the honest moment for a claim about startup.

    Worded as a point-in-time observation, not an instruction: a user who signs in mid-session
    makes it stale, and every tool re-reads the credential file on each call, so the envelope
    is always the authoritative answer. Its value is purely proactive — without it the agent
    cannot know to offer sign-in until the first edit has already failed. Deliberately carries
    no email: it lands in the model's context on every run.
    """
    try:
        credentials = runtime_config.load_credentials()
    except Exception:  # noqa: BLE001 — a diagnostic line must never block server startup
        return ""
    if not credentials.get("email") or not credentials.get("password"):
        return (
            " At startup this machine had no Cassette credentials stored: say so before ingesting "
            "media and offer to sign in, rather than letting the first edit fail."
        )
    verified = str(credentials.get("verified_at") or "").strip()
    return f" At startup this machine was signed in to Cassette{f' (verified {verified})' if verified else ''}."


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


def _client_supports_roots(context: Context) -> bool:
    """Did the client advertise the roots capability during initialize?

    Read from the negotiated client_params rather than remembered separately, so the
    capability the server acts on is always the one it reported on stderr.
    """
    params = getattr(getattr(context, "session", None), "client_params", None)
    capabilities = getattr(params, "capabilities", None)
    return getattr(capabilities, "roots", None) is not None


def _log_client_profile_once(context: Context) -> None:
    """Record the negotiated protocol revision and optional capabilities, once per run.

    The elicitation and roots paths below both degrade to silence by design.  That was
    safe while every host spoke one revision, but 2026-07-28 removes the `initialize`
    handshake those capability probes read, so against a new-spec client they become
    permanent no-ops that look identical to a broken server.  Naming the negotiated
    profile once, on stderr, is what turns "elicitation stopped working after a host
    upgrade" into a single line the user can read back.
    """
    try:
        lifespan = context.request_context.lifespan_context
        if lifespan.client_profile_logged:
            return
        lifespan.client_profile_logged = True
        params = getattr(context.session, "client_params", None)
        capabilities = getattr(params, "capabilities", None)
        info = getattr(params, "clientInfo", None)
        negotiated = getattr(params, "protocolVersion", None)
        elicitation = getattr(capabilities, "elicitation", None) is not None
        roots = _client_supports_roots(context)
        print(
            "oh-my-cassette: mcp client={name}/{version} protocol={negotiated} "
            "server_max={supported} elicitation={elicitation} roots={roots}".format(
                name=getattr(info, "name", None) or "unknown",
                version=getattr(info, "version", None) or "unknown",
                negotiated=negotiated or "unnegotiated",
                supported=types.LATEST_PROTOCOL_VERSION,
                elicitation="yes" if elicitation else "no",
                roots="yes" if roots else "no",
            ),
            file=sys.stderr,
            flush=True,
        )
        if not elicitation:
            print(
                "oh-my-cassette: this client offers no elicitation capability; needs_user "
                "questions fall back to the cassette_answer_question round-trip",
                file=sys.stderr,
                flush=True,
            )
        if not roots:
            print(
                "oh-my-cassette: this client offers no roots capability; media paths must "
                "come from CASSETTE_PROJECT_ROOT or a configured media root",
                file=sys.stderr,
                flush=True,
            )
    except Exception:  # noqa: BLE001 — diagnostics must never fail a tool call
        return


class ArtifactFastMCP(FastMCP[McpLifespanContext]):
    """Append validated artifact ResourceLink blocks to structured tool output."""

    def _register_artifact_resource(self, artifact: dict[str, Any]) -> None:
        """Make a validated local artifact readable through MCP resources/read.

        ResourceLink is a pointer, not a file server.  Hosts such as Hermes follow the
        pointer with resources/read, so every link emitted below must have a matching
        concrete resource for the lifetime of this stdio server.  The runtime already
        validates artifact paths; repeat the boundary check here so a future producer
        cannot accidentally expose an arbitrary local file through this generic hook.
        """
        raw_path = str(artifact.get("path") or "").strip()
        raw_uri = str(artifact.get("resource_uri") or artifact.get("uri") or "").strip()
        if not raw_path or not raw_uri:
            return
        try:
            resolved = Path(raw_path).expanduser().resolve(strict=True)
            asset_root = runtime_config.asset_root().resolve(strict=True)
            allowed_roots = (asset_root / "previews", asset_root / "exports")
            if (
                not resolved.is_file()
                or not any(resolved.is_relative_to(root.resolve(strict=False)) for root in allowed_roots)
                or raw_uri != resolved.as_uri()
            ):
                return
            mime_type = str(artifact.get("mime_type") or "application/octet-stream")
            self.add_resource(
                FileResource(
                    uri=raw_uri,
                    name=str(artifact.get("name") or resolved.name),
                    description="Validated Cassette artifact",
                    mime_type=mime_type,
                    path=resolved,
                    is_binary=not mime_type.startswith("text/"),
                )
            )
        except (OSError, ValueError):
            return

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        try:
            _log_client_profile_once(self.get_context())
        except Exception:  # noqa: BLE001 — never let diagnostics block the tool surface
            pass
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
            self._register_artifact_resource(artifact)
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
    # Both trailing notices are empty in the common case: notice() unless a newer release is
    # already cached on disk, auto_update_notice() unless this is Claude Code with the
    # marketplace toggle still off. Only Claude Code updates plugins on its own.
    instructions=(
        "Local video-editing MCP runtime for Oh My Cassette. It uses stdio, opens no port, "
        "and connects directly to the separate Cassette backend. "
        "Courier doctrine: you relay a direct multi-turn conversation between the user and the "
        "Cassette agent. Call cassette_ingest_media once per source file (reuse the returned "
        "session_id), then for every editing request call cassette_run_job with message set to "
        "the user's VERBATIM words — never rewrite, optimize, or expand them; the agent reads "
        "the session's media itself (cassette_make_prompt is a legacy brief builder — do not call it). "
        "One session = one persistent agent thread with memory. "
        "A turn ends succeeded with the edit committed and nothing rendered, carrying "
        "timeline_delta + quality.timeline_ctl + a contact-sheet artifact as the per-turn "
        "preview; pass export=true only when the user expresses finish/export intent. "
        "Model/thinking: never ask upfront (defaults match the web editor); when the user asks, "
        "set them via cassette_config — applied from the next turn. "
        "cassette_run_job returns when the turn is settled, streaming progress notifications while it "
        "works — one call per turn, never a status loop. Route on the typed phase and next_action "
        "fields, never on prose: needs_user means ask the user then call cassette_answer_question; "
        "review_required (export turns) means evaluate the result and call cassette_review_completion in the "
        "same assistant turn (only decision=export renders); an explicit user export request is already "
        "authorization, so do not ask them to confirm again merely because Cassette's prose says it cannot "
        "render; succeeded means relay the delta/preview and continue the "
        "conversation; exported means present the validated artifacts; "
        "failed, cancelled, or timed_out means report the structured error (thread_busy = a run "
        "is already live on this session's thread; wait and retry). cassette_job_status is for "
        "re-attaching to a job whose call did not return (host restart, cancellation, wait=false) — "
        "call it once, never in a loop. "
        "Ground every statement about project state in cassette_timeline, never in memory, and "
        "name the version in replies. Small named edits (trim, text, delete, undo) go through "
        "cassette_edit when CASSETTE_DIRECT_EDIT=1: read the timeline first, pass "
        "expected_version, and on stale_timeline re-read and retry; creative or multi-step "
        "briefs go through cassette_run_job. Envelopes carry timeline_delta (what changed) and plan_progress — "
        "relay them instead of re-describing state. A needs_user question with reason "
        "edit_plan_review is the edit plan itself (quality also carries storyboard beat cells "
        "and a storyboard_sheet image — one planned source frame per beat, zero render): relay "
        "it with the link and answer via "
        "cassette_answer_question with approve, revise <feedback>, or reject; if the resume "
        "returns resume_not_waiting_for_user the user already decided in the editor tab — "
        "re-check status. "
        "If a tool returns auth_required, read error.details.recovery and act on the entry whose "
        "'when' matches the user: cassette_login with the generated password from their Cassette "
        "email; or, once they confirm a replacement, cassette_login with request_new_password=true "
        "and confirm_replace=true; or error.details.setup_command as a private terminal command if "
        "they would rather keep the password out of this conversation. Cassette passwords are always "
        "server-generated — never invent or guess one, always ask the user for it."
        + _startup_auth_notice()
        + update_check.notice()
        + update_check.auto_update_notice()
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
    if not _client_supports_roots(context):
        # Asking anyway is not a harmless no-op. A client that never advertised roots is
        # under no obligation to answer roots/list, and the await below has no deadline of
        # its own, so a client that simply drops the request leaves cassette_ingest_media
        # blocked forever. Honouring the negotiated capability keeps the degrade graceful.
        return roots
    try:
        result = await asyncio.wait_for(context.session.list_roots(), timeout=_ROOTS_TIMEOUT_SEC)
    except Exception:  # client root support is optional, and a slow client must not wedge ingest
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
    envelope = await _run_sync(_runtime(ctx).ingest_media, request.model_dump(exclude_none=True), roots)
    # Refresh the cached release version off the hot path: ingest already waits on the
    # network, and the day-long TTL means this is a no-op on all but the first call.
    await asyncio.to_thread(update_check.refresh)
    return envelope


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
        "the stored clip posters into one image (zero render) and saves it locally. Present the "
        "returned contact_sheet_uri as the thumbnail link. In Hermes TUI, label it as saved locally "
        "and output MEDIA:<contact_sheet_uri> on its own line; use the URL-encoded URI, never the raw "
        "contact_sheet_path, because raw paths may contain spaces."
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
        "Run one Cassette edit turn and return when it is settled. This call IS the wait: it streams "
        "progress notifications while the agent works and answers with the terminal envelope "
        "(succeeded / needs_user / review_required / exported / failed). Pass message as the user's words "
        "verbatim. Exactly one cassette_run_job call per user turn: after any settled result, return "
        "control to the user. Never start a corrective, retry, or follow-up run in the same user turn, "
        "even if you think the edit could be improved. Do not poll cassette_job_status after a settled "
        "result; that tool is only for resuming a job whose call was interrupted. Pass wait=false only "
        "to deliberately detach the turn into the background. If an explicit export request returns "
        "phase=review_required, the user has already authorized export: inspect the attached timeline and "
        "call cassette_review_completion in this same assistant turn. Do not ask for redundant confirmation, "
        "and do not start another cassette_run_job."
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
    wait: bool = True,
    timeout_sec: int | None = None,
    selectors: dict[str, Any] | None = None,
    cassette_model: str | None = None,
    model: str | None = None,
    thinking_level: Literal["off", "minimal", "low", "medium", "high", "xhigh"] | None = None,
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
    loop = asyncio.get_running_loop()

    def _tick(elapsed: float, stage: str) -> None:
        # report_progress is a no-op unless the client sent a progressToken. Emitting it also
        # keeps the call off the host's idle-abort path, which is what lets one blocking call
        # stand in for a poll loop.
        asyncio.run_coroutine_threadsafe(ctx.report_progress(elapsed, None, stage or None), loop)

    envelope = await _run_sync(_runtime(ctx).run_job, request.model_dump(exclude_none=True), _tick if wait else None)
    return await _maybe_elicit_needs_user(ctx, envelope)


@mcp.tool(
    description=(
        "Re-attach to a job whose cassette_run_job call did not return — host restart, cancellation, "
        "or a turn deliberately started with wait=false. Call it once and act on the phase; it is not "
        "a progress loop. wait_for_change_sec gives an unsettled job up to 30 seconds to reach its next "
        "phase before answering."
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
        "Resolve a review-required completion. Rendering starts only for an explicit, validated decision=export. "
        "For a cassette_run_job(export=true) triggered by the user's explicit export request, review the attached "
        "timeline and call this immediately in the same assistant turn; do not ask the user to authorize export "
        "again merely because the Cassette agent's prose claims rendering is unavailable."
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


@mcp.tool(
    description=(
        "Get or set the session's Cassette model and thinking level. Call with only session_id to "
        "see the current choice and available options; pass model (id or label) and/or "
        "thinking_level to change them — persisted for the session, applied from the next "
        "cassette_run_job turn. Defaults match the web editor; change only when the user asks."
    ),
    structured_output=True,
)
async def cassette_config(
    session_id: str,
    ctx: Context,
    model: str | None = None,
    thinking_level: Literal["off", "minimal", "low", "medium", "high", "xhigh"] | None = None,
) -> ToolEnvelope:
    request = ConfigInput(session_id=session_id, model=model, thinking_level=thinking_level)
    return await _run_sync(
        _runtime(ctx).simple_session_tool,
        "cassette_config",
        request.model_dump(exclude_none=True),
    )


@mcp.tool(
    description=(
        "Sign this machine in to Cassette, or have a replacement password emailed. Pass email plus "
        "the generated password from the user's Cassette email. Cassette passwords are always "
        "server-generated, never chosen — ask the user for theirs, never invent one. If they no "
        "longer have it, confirm with them first, then pass request_new_password=true with "
        "confirm_replace=true: that replaces the account password on every machine they use and "
        "emails a new one. Credentials are verified before anything is written, so a wrong password "
        "leaves an existing working setup untouched."
    ),
    structured_output=True,
)
async def cassette_login(
    email: str,
    ctx: Context,
    password: str | None = None,
    request_new_password: bool = False,
    confirm_replace: bool = False,
) -> ToolEnvelope:
    request = LoginInput(
        email=email,
        password=password,
        request_new_password=request_new_password,
        confirm_replace=confirm_replace,
    )
    # exclude_none only: the two booleans must survive as False so the core handler can tell
    # "not confirmed" from "absent" without re-deriving intent.
    return await _run_sync(_runtime(ctx).login, request.model_dump(exclude_none=True))


def main() -> None:
    try:
        from cassette.core import manifest as _manifest

        _manifest.sweep_stale_artifacts()
    except Exception:  # noqa: BLE001 — a failed sweep must never block the server
        pass
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
