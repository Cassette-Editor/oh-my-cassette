"""Host-neutral adapter from typed MCP calls to the existing Cassette core."""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import time
from pathlib import Path
from typing import Any, Callable

import runtime_config

from .core_loader import PLUGIN_ROOT, load_core
from .models import Artifact, SessionPhase, ToolEnvelope, ToolErrorInfo
from .state import InvalidTransition, StateStore, next_action_for, phase_from_job


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class LocalMcpRuntime:
    """One process-local MCP runtime; persisted jobs and state live in shared storage."""

    def __init__(self, config_errors: list[runtime_config.RuntimeConfigError] | None = None):
        load_core()
        from cassette import jobs, tools

        self.jobs = jobs
        self.tools = tools
        self.config_errors = list(config_errors or [])
        if self.config_errors:
            self.state = None
            return
        try:
            self.state: StateStore | None = StateStore()
        except runtime_config.RuntimeConfigError as exc:
            self.config_errors.append(exc)
            self.state = None

    def _config_error(self, *, session_id: str | None = None) -> ToolEnvelope | None:
        if not self.config_errors:
            return None
        issue = self.config_errors[0]
        return self._failure(
            "config_security_error",
            str(issue),
            details={"config_code": issue.code, "path": str(issue.path or "")},
            session_id=session_id,
        )

    def _auth_error(self, *, session_id: str | None = None) -> ToolEnvelope | None:
        try:
            credentials = runtime_config.load_credentials()
        except runtime_config.RuntimeConfigError as exc:
            return self._failure(
                "config_security_error",
                str(exc),
                details={"config_code": exc.code, "path": str(exc.path or "")},
                session_id=session_id,
            )
        if not credentials.get("email") or not credentials.get("password"):
            command = runtime_config.setup_command(PLUGIN_ROOT)
            return self._failure(
                "auth_required",
                "Cassette authentication is required for this operation.",
                details={"setup_command": command},
                session_id=session_id,
            )
        selected = str(os.getenv("CASSETTE_TRANSPORT", "api") or "api").strip().lower()
        if selected != "browser" and credentials.get("full_api_access") is False:
            return self._failure(
                "api_access_unavailable",
                "This account does not have full Cassette API access; configure the optional browser transport.",
                details={"browser_setup_command": runtime_config.browser_setup_command(PLUGIN_ROOT)},
                session_id=session_id,
            )
        return None

    def _redaction_secrets(self) -> list[str]:
        values: list[str] = []
        try:
            credentials = runtime_config.load_credentials()
            for key in ("email", "password"):
                value = str(credentials.get(key) or "")
                if value:
                    values.append(value)
        except runtime_config.RuntimeConfigError:
            pass
        return values

    def _redact(self, value: Any) -> Any:
        secrets_to_hide = self._redaction_secrets()

        def visit(item: Any) -> Any:
            if isinstance(item, dict):
                return {
                    key: visit(child)
                    for key, child in item.items()
                    if not str(key).lower().startswith("hermes_")
                    and str(key).lower() not in {"continuation", "resume_request", "worker_command"}
                }
            if isinstance(item, list):
                return [visit(child) for child in item]
            if isinstance(item, str):
                result = item
                for secret in secrets_to_hide:
                    result = result.replace(secret, "<redacted>")
                # Normalize only legacy core-owned machine labels and canned
                # messages at the MCP boundary. User text is otherwise preserved.
                replacements = {
                    "completion_requires_hermes_review": "completion_requires_review",
                    "hermes_completion_review_failed": "completion_review_failed",
                    "hermes_completion_review_continue": "completion_review_continue",
                    "hermes_completion_review_needs_user": "completion_review_needs_user",
                    "hermes_completion_review": "completion_review",
                    "Hermes judged that Cassette did not complete the edit.": (
                        "The host agent judged that Cassette did not complete the edit."
                    ),
                }
                result = replacements.get(result, result)
                return result
            return item

        return visit(value)

    def _load_state_phase(self, session_id: str | None) -> SessionPhase:
        if not session_id or self.state is None:
            return SessionPhase.NEW
        return self.state.load(session_id).phase

    def _failure(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        recoverable: bool = True,
        session_id: str | None = None,
        job_id: str | None = None,
        phase: SessionPhase | None = None,
    ) -> ToolEnvelope:
        selected_phase = phase or self._load_state_phase(session_id)
        return ToolEnvelope(
            ok=False,
            error=ToolErrorInfo(
                code=code,
                message=str(self._redact(message)),
                details=self._redact(details or {}),
                recoverable=recoverable,
            ),
            session_id=session_id,
            job_id=job_id,
            phase=selected_phase,
            next_action=next_action_for(selected_phase, job_id=job_id),
        )

    def _transition(
        self,
        session_id: str,
        phase: SessionPhase,
        *,
        job_id: str | None = None,
    ) -> SessionPhase:
        if self.state is None:
            return phase
        return self.state.transition(session_id, phase, job_id=job_id).phase

    def _sync_job(self, session_id: str, job: dict) -> SessionPhase:
        if self.state is None:
            return phase_from_job(job)
        return self.state.sync_job(session_id, job).phase

    def _session_for_job(self, job: dict, fallback: str | None = None) -> str | None:
        value = str(job.get("cassette_session_id") or fallback or "").strip()
        return value or None

    def _artifacts_for_job(self, job: dict) -> tuple[list[Artifact], ToolEnvelope | None]:
        job_id = str(job.get("job_id") or "").strip()
        asset_root = runtime_config.asset_root()
        expected_root = Path(os.path.abspath(str(asset_root / "exports" / job_id)))
        artifacts: list[Artifact] = []
        for index, output in enumerate(job.get("outputs") or []):
            if not isinstance(output, dict) or not output.get("local_path"):
                continue
            raw = Path(str(output["local_path"])).expanduser()
            try:
                if not raw.is_absolute():
                    raise OSError("artifact path is not absolute")
                lexical = Path(os.path.abspath(str(raw)))
                if not _is_relative_to(lexical, expected_root):
                    raise OSError("artifact path is outside the job export directory")
                relative = lexical.relative_to(asset_root)
                cursor = asset_root
                if cursor.is_symlink():
                    raise OSError("artifact root is a symlink")
                for part in relative.parts:
                    cursor = cursor / part
                    if cursor.is_symlink():
                        raise OSError("artifact path contains a symlink")
                resolved = raw.resolve(strict=True)
                resolved_expected_root = expected_root.resolve(strict=True)
                if not resolved.is_file() or not _is_relative_to(resolved, resolved_expected_root):
                    raise OSError("artifact is outside the job export directory")
                size = resolved.stat().st_size
            except OSError as exc:
                return [], self._failure(
                    "output_path_not_allowed",
                    "Cassette produced an output path outside its validated export directory.",
                    details={"reason": str(exc), "output_index": index},
                    recoverable=False,
                    session_id=self._session_for_job(job),
                    job_id=job_id,
                    phase=phase_from_job(job),
                )
            mime = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
            uri = resolved.as_uri()
            artifacts.append(
                Artifact(
                    path=str(resolved),
                    uri=uri,
                    resource_uri=uri,
                    mime_type=mime,
                    size=size,
                    name=resolved.name,
                )
            )
        return artifacts, None

    def _invoke_core(
        self,
        name: str,
        args: dict[str, Any],
        *,
        session_id: str | None,
        roots: list[Path] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        handler: Callable[..., str] = getattr(self.tools, name)
        with runtime_config.temporary_media_roots(roots or []):
            raw = handler(args, runtime_host="mcp", **kwargs)
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return {
                "ok": False,
                "error": {
                    "code": "invalid_core_result",
                    "message": "Cassette core returned a malformed result.",
                    "details": {},
                    "recoverable": False,
                },
            }
        return self._redact(payload)

    def _envelope_from_core(
        self,
        payload: dict[str, Any],
        *,
        session_id: str | None,
        job_id: str | None = None,
        phase: SessionPhase | None = None,
        artifacts: list[Artifact] | None = None,
    ) -> ToolEnvelope:
        resolved_job_id = str(payload.get("job_id") or job_id or "").strip() or None
        selected_phase = phase or self._load_state_phase(session_id)
        if not payload.get("ok"):
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            code = str(error.get("code") or "core_error")
            if code == "source_path_outside_allowed_roots":
                code = "source_path_not_allowed"
            return self._failure(
                code,
                str(error.get("message") or "Cassette operation failed."),
                details=error.get("details") if isinstance(error.get("details"), dict) else {},
                recoverable=bool(error.get("recoverable", True)),
                session_id=session_id,
                job_id=resolved_job_id,
                phase=selected_phase,
            )
        return ToolEnvelope(
            ok=True,
            data=payload.get("data") if isinstance(payload.get("data"), dict) else {},
            warnings=payload.get("warnings") if isinstance(payload.get("warnings"), list) else [],
            session_id=session_id,
            job_id=resolved_job_id,
            phase=selected_phase,
            next_action=next_action_for(selected_phase, job_id=resolved_job_id),
            artifacts=artifacts or [],
        )

    def ingest_media(self, args: dict[str, Any], roots: list[Path]) -> ToolEnvelope:
        config_error = self._config_error(session_id=args.get("session_id"))
        if config_error:
            return config_error
        session_id = str(args.get("session_id") or "").strip() or f"mcp_{secrets.token_urlsafe(18)}"
        args = {**args, "session_id": session_id}
        payload = self._invoke_core("cassette_ingest_media", args, session_id=session_id, roots=roots)
        phase = self._load_state_phase(session_id)
        if payload.get("ok"):
            try:
                phase = self._transition(session_id, SessionPhase.ASSETS_READY)
            except InvalidTransition as exc:
                return self._failure("invalid_transition", str(exc), session_id=session_id, phase=exc.current)
        return self._envelope_from_core(payload, session_id=session_id, phase=phase)

    def list_assets(self, args: dict[str, Any]) -> ToolEnvelope:
        session_id = str(args.get("session_id") or "").strip() or None
        config_error = self._config_error(session_id=session_id)
        if config_error:
            return config_error
        payload = self._invoke_core("cassette_list_assets", args, session_id=session_id)
        phase = self._load_state_phase(session_id)
        manifest = payload.get("data", {}).get("manifest") if payload.get("ok") else None
        if session_id and isinstance(manifest, dict) and manifest.get("assets"):
            try:
                phase = self._transition(session_id, SessionPhase.GUIDED_CHOICES)
            except InvalidTransition as exc:
                return self._failure("invalid_transition", str(exc), session_id=session_id, phase=exc.current)
        return self._envelope_from_core(payload, session_id=session_id, phase=phase)

    def make_prompt(self, args: dict[str, Any]) -> ToolEnvelope:
        session_id = str(args.get("session_id") or "").strip() or None
        config_error = self._config_error(session_id=session_id)
        if config_error:
            return config_error
        payload = self._invoke_core("cassette_make_prompt", args, session_id=session_id)
        phase = self._load_state_phase(session_id)
        if payload.get("ok") and session_id:
            try:
                phase = self._transition(session_id, SessionPhase.READY)
            except InvalidTransition as exc:
                return self._failure("invalid_transition", str(exc), session_id=session_id, phase=exc.current)
        return self._envelope_from_core(payload, session_id=session_id, phase=phase)

    def answer_question(self, args: dict[str, Any]) -> ToolEnvelope:
        job_id = str(args.get("job_id") or "").strip() or None
        config_error = self._config_error()
        if config_error:
            config_error.job_id = job_id
            return config_error
        if not job_id:
            payload = self._invoke_core("cassette_answer_question", args, session_id=None)
            return self._envelope_from_core(payload, session_id=None)
        try:
            job = self.jobs.load_job(job_id)
        except Exception:  # core handler will provide the canonical error shape
            payload = self._invoke_core("cassette_answer_question", args, session_id=None)
            return self._envelope_from_core(payload, session_id=None, job_id=job_id)
        session_id = self._session_for_job(job)
        auth_error = self._auth_error(session_id=session_id)
        if auth_error:
            auth_error.job_id = job_id
            return auth_error
        phase = phase_from_job(job)
        if phase != SessionPhase.NEEDS_USER:
            return self._failure(
                "invalid_transition",
                f"Job {job_id} cannot resume from phase {phase.value}.",
                session_id=session_id,
                job_id=job_id,
                phase=phase,
            )
        payload = self._invoke_core("cassette_answer_question", args, session_id=session_id)
        try:
            updated = self.jobs.load_job(job_id)
            phase = self._sync_job(session_id or "", updated) if session_id else phase_from_job(updated)
        except Exception:
            updated = job
        if phase == SessionPhase.FAILED:
            errors = updated.get("errors") if isinstance(updated.get("errors"), list) else []
            latest = errors[-1] if errors and isinstance(errors[-1], dict) else {}
            return self._failure(
                str(latest.get("code") or "job_resume_failed"),
                str(latest.get("message") or "Cassette could not resume the paused job."),
                details=latest.get("details") if isinstance(latest.get("details"), dict) else {},
                session_id=session_id,
                job_id=job_id,
                phase=phase,
            )
        artifacts, artifact_error = self._artifacts_for_job(updated)
        if artifact_error:
            return artifact_error
        return self._envelope_from_core(payload, session_id=session_id, job_id=job_id, phase=phase, artifacts=artifacts)

    def simple_session_tool(self, name: str, args: dict[str, Any]) -> ToolEnvelope:
        session_id = str(args.get("session_id") or "").strip() or None
        config_error = self._config_error(session_id=session_id)
        if config_error:
            return config_error
        payload = self._invoke_core(name, args, session_id=session_id)
        return self._envelope_from_core(payload, session_id=session_id)

    def run_job(self, args: dict[str, Any]) -> ToolEnvelope:
        session_id = str(args.get("session_id") or "").strip() or None
        config_error = self._config_error(session_id=session_id)
        if config_error:
            return config_error
        auth_error = self._auth_error(session_id=session_id)
        if auth_error:
            return auth_error
        if not session_id:
            return self._failure(
                "session_id_required",
                "cassette_run_job requires the session_id returned by cassette_ingest_media.",
                session_id=None,
                phase=SessionPhase.NEW,
            )
        current_phase = self._load_state_phase(session_id)
        if current_phase != SessionPhase.READY:
            return self._failure(
                "invalid_transition",
                f"Cassette job execution requires phase ready; current phase is {current_phase.value}.",
                session_id=session_id,
                phase=current_phase,
            )
        payload = self._invoke_core("cassette_run_job", args, session_id=session_id)
        job_id = str(payload.get("job_id") or "").strip() or None
        phase = self._load_state_phase(session_id)
        job: dict[str, Any] = {}
        if payload.get("ok") and job_id:
            try:
                job = self.jobs.load_job(job_id)
                session_id = self._session_for_job(job, session_id)
                if session_id:
                    current = self._load_state_phase(session_id)
                    if current != SessionPhase.RUNNING:
                        self._transition(session_id, SessionPhase.RUNNING, job_id=job_id)
                    phase = self._sync_job(session_id, job)
                else:
                    phase = phase_from_job(job)
            except InvalidTransition as exc:
                return self._failure(
                    "invalid_transition",
                    str(exc),
                    session_id=session_id,
                    job_id=job_id,
                    phase=exc.current,
                )
            except Exception:
                pass
        artifacts, artifact_error = self._artifacts_for_job(job) if job else ([], None)
        if artifact_error:
            return artifact_error
        return self._envelope_from_core(payload, session_id=session_id, job_id=job_id, phase=phase, artifacts=artifacts)

    @staticmethod
    def _job_change_marker(job: dict[str, Any]) -> tuple[Any, ...]:
        quality = job.get("quality") if isinstance(job.get("quality"), dict) else {}
        return (
            job.get("updated_at"),
            job.get("status"),
            job.get("current_stage"),
            quality.get("current_stage"),
            quality.get("progress_summary"),
            len(job.get("outputs") or []),
            len(job.get("questions") or []),
            len(job.get("errors") or []),
            len(job.get("progress_events") or []),
        )

    def job_status(self, args: dict[str, Any]) -> ToolEnvelope:
        job_id = str(args.get("job_id") or "").strip() or None
        session_id = str(args.get("session_id") or "").strip() or None
        config_error = self._config_error(session_id=session_id)
        if config_error:
            config_error.job_id = job_id
            return config_error
        wait_sec = min(30.0, max(0.0, float(args.pop("wait_for_change_sec", 0.0) or 0.0)))
        if job_id and wait_sec:
            try:
                baseline = self._job_change_marker(self.jobs.load_job(job_id))
                deadline = time.monotonic() + wait_sec
                while time.monotonic() < deadline:
                    remaining = deadline - time.monotonic()
                    time.sleep(min(0.25, max(0.01, remaining)))
                    if self._job_change_marker(self.jobs.load_job(job_id)) != baseline:
                        break
            except Exception:
                pass
        payload = self._invoke_core("cassette_job_status", args, session_id=session_id)
        phase = self._load_state_phase(session_id)
        job: dict[str, Any] = {}
        if job_id:
            try:
                job = self.jobs.load_job(job_id)
                session_id = self._session_for_job(job, session_id)
                phase = self._sync_job(session_id, job) if session_id else phase_from_job(job)
            except InvalidTransition as exc:
                return self._failure(
                    "invalid_transition", str(exc), session_id=session_id, job_id=job_id, phase=exc.current
                )
            except Exception:
                pass
        artifacts, artifact_error = self._artifacts_for_job(job) if job else ([], None)
        if artifact_error:
            return artifact_error
        return self._envelope_from_core(payload, session_id=session_id, job_id=job_id, phase=phase, artifacts=artifacts)

    def review_completion(self, args: dict[str, Any]) -> ToolEnvelope:
        job_id = str(args.get("job_id") or "").strip()
        config_error = self._config_error()
        if config_error:
            config_error.job_id = job_id
            return config_error
        try:
            job = self.jobs.load_job(job_id)
        except Exception:
            payload = self._invoke_core("cassette_review_completion", args, session_id=None)
            return self._envelope_from_core(payload, session_id=None, job_id=job_id)
        session_id = self._session_for_job(job)
        phase = phase_from_job(job)
        if phase != SessionPhase.REVIEW_REQUIRED:
            return self._failure(
                "invalid_transition",
                f"Completion review is only valid in review_required; current phase is {phase.value}.",
                session_id=session_id,
                job_id=job_id,
                phase=phase,
            )
        auth_error = self._auth_error(session_id=session_id)
        if auth_error:
            auth_error.job_id = job_id
            auth_error.phase = phase
            return auth_error
        if args.get("decision") == "export" and session_id:
            try:
                self._transition(session_id, SessionPhase.EXPORTING, job_id=job_id)
            except InvalidTransition as exc:
                return self._failure(
                    "invalid_transition", str(exc), session_id=session_id, job_id=job_id, phase=exc.current
                )
        payload = self._invoke_core("cassette_review_completion", args, session_id=session_id)
        updated = self.jobs.load_job(job_id)
        try:
            phase = self._sync_job(session_id, updated) if session_id else phase_from_job(updated)
        except InvalidTransition as exc:
            return self._failure(
                "invalid_transition", str(exc), session_id=session_id, job_id=job_id, phase=exc.current
            )
        artifacts, artifact_error = self._artifacts_for_job(updated)
        if artifact_error:
            return artifact_error
        return self._envelope_from_core(payload, session_id=session_id, job_id=job_id, phase=phase, artifacts=artifacts)

    def cancel_job(self, args: dict[str, Any]) -> ToolEnvelope:
        job_id = str(args.get("job_id") or "").strip()
        config_error = self._config_error()
        if config_error:
            config_error.job_id = job_id
            return config_error
        try:
            original = self.jobs.load_job(job_id)
            session_id = self._session_for_job(original)
        except Exception:
            session_id = None
        payload = self._invoke_core("cassette_cancel_job", args, session_id=session_id)
        phase = self._load_state_phase(session_id)
        if payload.get("ok"):
            try:
                updated = self.jobs.load_job(job_id)
                phase = self._sync_job(session_id, updated) if session_id else phase_from_job(updated)
            except Exception:
                phase = SessionPhase.RUNNING
        return self._envelope_from_core(payload, session_id=session_id, job_id=job_id, phase=phase)
