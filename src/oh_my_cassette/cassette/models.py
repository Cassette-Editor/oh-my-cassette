"""Pydantic mirrors of the Cassette-Editor wire contracts this plugin depends on.

Sources (Cassette-Editor `main`):
  - server/agent-openai/routes/http-types.ts      (commands, state, errors)
  - server/agent-openai/events/agent-run-events.ts (durable / transient run events)
  - packages/shared/src/media-workspace/{imports,uploads}.ts
  - packages/shared/src/types/media-upload.ts      (video preparation report)
  - packages/shared/src/timeline-document/client-contract.ts (project snapshot envelope)
  - server/services/job-store.ts                   (export job progress)

Models are deliberately permissive (`extra="allow"`) on responses so the plugin keeps working when
the backend adds fields, and strict on requests so a typo never reaches the wire.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

AgentRunMode = Literal["chat", "plan", "auto"]
AgentRunStatus = Literal[
    "running", "abort_requested", "interrupt_pending", "aborted", "completed", "not_done", "failed"
]
ReasoningEffort = Literal["none", "low", "medium", "high", "xhigh", "max"]

MODEL_IDS: tuple[str, ...] = ("openai/gpt-5.6-luna", "openai/gpt-5.6-terra", "openai/gpt-5.6-sol")
DEFAULT_MODEL_ID = MODEL_IDS[0]
REASONING_EFFORTS: tuple[str, ...] = ("none", "low", "medium", "high", "xhigh", "max")
DEFAULT_REASONING_EFFORT = "medium"

DURABLE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "run_started",
        "assistant_message_started",
        "assistant_message_committed",
        "tool_call_started",
        "tool_result",
        "task_snapshot",
        "compaction_marker",
        "interrupt_created",
        "interrupt_resolved",
        "run_paused",
        "run_resumed",
        "steering_delivered",
        "project_commit",
        "run_terminal",
        "run_aborted",
        "run_failed",
    }
)
TERMINAL_EVENT_TYPES: frozenset[str] = frozenset({"run_terminal", "run_aborted", "run_failed"})


class _Response(BaseModel):
    model_config = ConfigDict(extra="allow")


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ── Projects ──


class ProjectMetadata(_Response):
    id: str
    title: str | None = None


class ChatSession(_Response):
    id: str
    mode: AgentRunMode = "chat"
    title: str | None = None
    status: str | None = None


class ProjectDocumentMeta(_Response):
    version: int
    historyCursorSequence: int


class ProjectSnapshot(_Response):
    kind: Literal["project_document_snapshot"]
    document: dict[str, Any]
    meta: ProjectDocumentMeta | None = None

    @property
    def version(self) -> int:
        return int(self.document.get("version", 0))


class EditReviewSide(_Response):
    revisionId: str
    documentVersion: int


class EditResult(_Response):
    runId: str
    inputMessageId: str
    status: str
    editCount: int
    before: EditReviewSide
    after: EditReviewSide


class TimelineHistoryGroup(_Response):
    groupId: str
    cursorSequence: int
    label: str
    source: str
    versionBefore: int
    versionAfter: int
    committedAt: str
    agentRunId: str | None = None


class TimelineHistory(_Response):
    groups: list[TimelineHistoryGroup]
    cursorSequence: int
    version: int


class TimelineHistoryOutcome(_Response):
    status: str  # ready | moved | idempotent_existing | stale | invalid | execution_rejected
    version: int | None = None
    historyCursorSequence: int | None = None
    reason: str | None = None


# ── Agent runtime ──


class RunMessage(_Request):
    id: str
    content: str
    createdAt: str


class EditorSnapshot(_Request):
    activeSequenceId: str
    selectedClipIds: list[str] = Field(default_factory=list)
    currentFrame: int = 0
    totalFrames: int = 0
    fps: float = 30
    isPlaying: bool = False
    zoom: float = 1
    sequenceTimebase: dict[str, int] = Field(default_factory=lambda: {"num": 30, "den": 1})


class StartAgentRunRequest(_Request):
    projectId: str
    chatSessionId: str
    mediaSessionId: str
    mode: AgentRunMode
    interactionPolicy: Literal["interactive", "non_interactive"] | None = None
    turnKind: Literal["conversation"] = "conversation"
    selectedModelId: str
    selectedReasoningEffort: ReasoningEffort
    locale: str
    message: RunMessage
    editorSnapshot: EditorSnapshot
    idempotencyKey: str


class StartRunResult(_Response):
    runId: str
    taskId: str
    chatSessionId: str
    status: AgentRunStatus
    eventCursor: int


class CommandResponse(_Response):
    state: Literal["applied", "replay", "conflict"]
    controlRevision: int
    result: dict[str, Any] | None = None


class PendingInterruption(_Response):
    kind: str
    interruptId: str | None = None
    pauseId: str | None = None


class ActiveRun(_Response):
    runId: str
    taskId: str
    mode: AgentRunMode
    status: Literal["running", "abort_requested", "interrupt_pending"]
    pendingInterruption: PendingInterruption | None = None


class ExecutionPreference(_Response):
    revision: int
    mode: AgentRunMode
    preferredModelId: str
    preferredReasoningEffort: str


class SessionState(_Response):
    chatSessionId: str
    projectId: str | None
    eventCursor: int
    controlRevision: int
    activeRun: ActiveRun | None = None
    executionPreference: ExecutionPreference | None = None
    projectActiveRuns: list[dict[str, Any]] = Field(default_factory=list)


class RunEvent(_Response):
    """A durable SSE frame (`id: <sequence>`)."""

    sequence: int
    runId: str
    chatSessionId: str
    projectId: str | None = None
    type: str
    payload: dict[str, Any] | None = None
    createdAt: str | None = None


class TransientEvent(_Response):
    runId: str
    type: str
    payload: dict[str, Any] | None = None


class AskUserChoice(_Response):
    id: str
    label: str


class AskUserQuestion(_Response):
    question: str
    reason: str = ""
    choices: list[AskUserChoice] = Field(default_factory=list)
    allowsFreeText: bool = False
    toolCallId: str | None = None


class EditReceipt(_Response):
    groupId: str
    documentVersion: int


class RunRecord(_Response):
    runId: str
    taskId: str
    chatSessionId: str
    projectId: str | None
    mode: AgentRunMode
    status: AgentRunStatus
    terminalReason: str | None = None
    committedEditCount: int = 0
    committedEditReceipts: list[EditReceipt] = Field(default_factory=list)


# ── Media import ──


class ImportFile(_Request):
    id: str
    rootId: str | None = None
    path: list[str] = Field(default_factory=list)
    name: str
    mimeType: str
    byteLength: int
    lastModified: int
    sha256: str
    kind: Literal["video", "audio", "image"] | None


class ImportRequest(_Request):
    sessionId: str
    batchId: str
    expectedRevision: int
    destinationId: str | None = None
    roots: list[dict[str, Any]] = Field(default_factory=list)
    directories: list[dict[str, Any]] = Field(default_factory=list)
    files: list[ImportFile]
    conflicts: dict[str, Any] = Field(default_factory=dict)
    duplicateEntries: Literal["keep", "skip"] = "keep"


class WorkspaceFile(_Response):
    id: str
    name: str
    kind: str
    byteLength: int = 0
    durationUs: int | None = None
    uploadStatus: str | None = None
    folderId: str | None = None


class ImportManifest(_Response):
    request: dict[str, Any]
    files: list[WorkspaceFile]
    rejected: list[dict[str, Any]] = Field(default_factory=list)
    duplicates: list[dict[str, Any]] = Field(default_factory=list)
    reservedBytes: int = 0


class UploadArtifactRequest(_Request):
    role: Literal["original", "canonical", "preview"]
    sha256: str
    byteLength: int
    mimeType: str


class UploadRequest(_Request):
    sessionId: str
    batchId: str
    mediaId: str
    uploadId: str
    replacesUploadId: str | None = None
    artifacts: list[UploadArtifactRequest]
    preparation: dict[str, Any] | None = None


class UploadArtifactTarget(_Response):
    role: str
    objectId: str | None = None
    uploadUrl: str
    uploadContentType: str


class RegisteredUpload(_Response):
    id: str
    media_file_id: str
    status: str
    writable_until: str | None = None
    error: Any = None


class UploadRegistration(_Response):
    upload: RegisteredUpload
    artifacts: list[UploadArtifactTarget] = Field(default_factory=list)


class MediaOperationStatus(_Response):
    mediaFileId: str
    fileName: str | None = None
    terminalState: str | None = None
    uploadStatus: str | None = None
    previewStatus: str | None = None
    analyzeStatus: str | None = None
    aiReady: bool | None = None
    playbackReady: bool | None = None
    fullyReady: bool | None = None
    analysisReady: bool | None = None
    readinessPhase: str | None = None
    errorMessage: str | None = None

    @property
    def failed(self) -> bool:
        return self.uploadStatus in ("failed", "cancelled") or self.terminalState in (
            "failed",
            "purged",
            "purging",
        )


class MediaFile(_Response):
    id: str
    file_name: str
    file_type: str
    upload_status: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    readinessPhase: str | None = None
    analysisReady: bool | None = None


# ── Export ──


class ExportJob(_Response):
    jobId: str
    status: Literal["queued", "bundling", "selecting", "rendering", "done", "error", "cancelled"]
    step: str | None = None
    progress: float = 0
    progressPercent: int = 0
    outputFileName: str
    error: str | None = None
    errorCode: str | None = None
    fileUrl: str | None = None
    statusUrl: str | None = None
