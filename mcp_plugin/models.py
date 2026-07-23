"""Typed MCP inputs and structured result contracts."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class SessionPhase(str, Enum):
    NEW = "new"
    GUIDED_CHOICES = "guided_choices"
    ASSETS_READY = "assets_ready"
    READY = "ready"
    RUNNING = "running"
    NEEDS_USER = "needs_user"
    REVIEW_REQUIRED = "review_required"
    EXPORTING = "exporting"
    EXPORTED = "exported"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class ToolErrorInfo(StrictModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    recoverable: bool = True


class Artifact(StrictModel):
    path: str
    uri: str
    resource_uri: str
    mime_type: str
    size: int = Field(ge=0)
    name: str


class ToolEnvelope(StrictModel):
    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error: ToolErrorInfo | None = None
    warnings: list[Any] = Field(default_factory=list)
    session_id: str | None = None
    job_id: str | None = None
    phase: SessionPhase
    next_action: str
    artifacts: list[Artifact] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_error_contract(self) -> "ToolEnvelope":
        if self.ok and self.error is not None:
            raise ValueError("successful envelopes cannot include an error")
        if not self.ok and self.error is None:
            raise ValueError("failed envelopes require an error")
        return self


class SessionState(StrictModel):
    session_id: str
    phase: SessionPhase = SessionPhase.NEW
    job_id: str | None = None
    revision: int = Field(default=0, ge=0)
    updated_at: str


class IngestMediaInput(StrictModel):
    source_path: str
    original_name: str | None = None
    media_type: Literal["video", "image", "audio", "file", "unknown"] | None = None
    chat_id: str | None = None
    user_id: str | None = None
    message_id: str | None = None
    chat_type: str | None = None
    thread_id: str | None = None
    platform: str | None = None
    caption: str | None = None
    session_id: str | None = None


class ListAssetsInput(StrictModel):
    session_id: str | None = None
    chat_id: str | None = None


class TimelineInput(StrictModel):
    session_id: str
    detail: str | None = None
    profile: Literal["aligned", "gateway"] | None = None
    contact_sheet: bool = False


class MakePromptInput(StrictModel):
    instruction: str
    session_id: str | None = None
    chat_id: str | None = None
    requires_assets: bool = True
    output_format: str | None = None
    duration: str | None = None
    style: str | None = None
    cassette_language: Literal["zh", "en"] | None = None
    language: Literal["zh", "en"] | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)


class AnswerQuestionInput(StrictModel):
    question: str | None = None
    instruction: str | None = None
    asset_count: int | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    job_id: str | None = None
    response: str | None = None

    @model_validator(mode="after")
    def validate_mode(self) -> "AnswerQuestionInput":
        classify = bool((self.question or "").strip())
        resume = bool((self.job_id or "").strip() or (self.response or "").strip())
        if classify == resume:
            raise ValueError("provide either question, or both job_id and response")
        if resume and not ((self.job_id or "").strip() and (self.response or "").strip()):
            raise ValueError("resume mode requires both job_id and response")
        return self


class MatchBgmInput(StrictModel):
    session_id: str
    instruction: str
    search_queries: list[str]
    optimization_enabled: bool = False
    continue_after_match: bool = True
    fallback_from: str | None = None
    fallback_reason: str | None = None


class MatchExactBgmInput(StrictModel):
    session_id: str
    instruction: str
    title: str
    songTitle: str | None = None
    song_title: str | None = None
    artist: str | None = None
    singer: str | None = None
    optimization_enabled: bool = False
    continue_after_match: bool = True
    download: bool = True


class JamendoMatcherInput(StrictModel):
    userQuery: str
    user_query: str | None = None
    searchTerms: list[str]
    search_terms: list[str] | None = None
    fuzzyTags: list[str] | None = None
    fuzzy_tags: list[str] | None = None
    excludeTerms: list[str] | None = None
    exclude_terms: list[str] | None = None
    vocalInstrumental: Literal["vocal", "instrumental"] | None = None
    vocalinstrumental: Literal["vocal", "instrumental"] | None = None
    searchPlan: dict[str, Any] | str | None = None
    search_plan: dict[str, Any] | str | None = None
    repairJson: dict[str, Any] | str | None = None
    download: bool = True
    seed: int | None = None
    limit: int | None = None
    limitOverride: int | None = None
    outputDir: str | None = None
    session_id: str | None = None


class RunJobInput(StrictModel):
    prompt: str
    chat_message: str | None = None
    cassette_message: str | None = None
    instruction: str | None = None
    session_id: str | None = None
    chat_id: str | None = None
    url: str | None = None
    wait: bool = False
    timeout_sec: int | None = None
    selectors: dict[str, Any] = Field(default_factory=dict)
    cassette_model: str | None = None
    model: str | None = None
    thinking_level: str | None = None
    cassette_language: Literal["zh", "en"] | None = None
    language: Literal["zh", "en"] | None = None


class JobStatusInput(StrictModel):
    job_id: str | None = None
    session_id: str | None = None
    limit: int = Field(default=10, ge=1, le=100)
    wait_for_change_sec: float = Field(default=0.0, ge=0.0, le=30.0)


class ReviewCompletionInput(StrictModel):
    job_id: str
    decision: Literal["export", "continue", "needs_user", "failed"]
    reason: str
    summary: str | None = None


class CancelJobInput(StrictModel):
    job_id: str
