"""Media import: reserve → register upload → PUT artifacts → complete → wait for readiness."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from oh_my_cassette.cassette.errors import CassetteError, HttpError
from oh_my_cassette.cassette.http import CassetteHttp
from oh_my_cassette.cassette.models import (
    ImportFile,
    ImportManifest,
    ImportRequest,
    MediaFile,
    MediaOperationStatus,
    RegisteredUpload,
    UploadArtifactRequest,
    UploadRegistration,
    UploadRequest,
)
from oh_my_cassette.cassette.prepare import MediaTools, media_kind, mime_type, sha256_file_async

ProgressCallback = Callable[[str, float | None], Awaitable[None]]


@dataclass
class ImportedItem:
    path: str
    name: str
    kind: str | None
    sha256: str | None = None
    byte_length: int = 0
    media_file_id: str | None = None
    upload_id: str | None = None
    status: str = "pending"  # pending | uploaded | ready | duplicate | rejected | failed
    reason: str | None = None
    duration_sec: float | None = None
    readiness: str | None = None
    ai_ready: bool = False


@dataclass
class ImportReport:
    batch_id: str
    items: list[ImportedItem] = field(default_factory=list)

    @property
    def ready(self) -> list[ImportedItem]:
        return [i for i in self.items if i.status == "ready"]


class MediaClient:
    def __init__(self, http: CassetteHttp, tools: MediaTools) -> None:
        self._http = http
        self._tools = tools

    async def workspace_revision(self, session_id: str) -> int:
        body = await self._http.get_json("/api/media/workspace", params={"sessionId": session_id})
        return int(body.get("revision", 0))

    async def list_files(self, session_id: str) -> list[MediaFile]:
        body = await self._http.get_json("/api/media/files", params={"session_id": session_id})
        return [MediaFile.model_validate(item) for item in body.get("files", [])]

    async def statuses(self, ids: list[str]) -> list[MediaOperationStatus]:
        if not ids:
            return []
        body = await self._http.get_json("/api/media/operations/status", params={"ids": ",".join(ids)})
        return [MediaOperationStatus.model_validate(item) for item in body.get("statuses", [])]

    async def import_paths(
        self,
        session_id: str,
        paths: list[Path],
        *,
        on_progress: ProgressCallback | None = None,
        ready_timeout_sec: float = 900,
        known_sha: dict[str, str] | None = None,
    ) -> ImportReport:
        async def progress(message: str, fraction: float | None = None) -> None:
            if on_progress is not None:
                await on_progress(message, fraction)

        report = ImportReport(batch_id=str(uuid.uuid4()))
        request_files: list[ImportFile] = []
        by_request_id: dict[str, ImportedItem] = {}
        for path in paths:
            item = ImportedItem(path=str(path), name=path.name, kind=media_kind(path))
            report.items.append(item)
            if not path.is_file():
                item.status, item.reason = "rejected", "file_not_found"
                continue
            if item.kind is None:
                item.status, item.reason = "rejected", "unsupported_type"
                continue
            item.byte_length = path.stat().st_size
            if item.byte_length == 0:
                item.status, item.reason = "rejected", "empty_file"
                continue
            await progress(f"hashing {path.name}")
            item.sha256 = await sha256_file_async(path)
            if known_sha and item.sha256 in known_sha:
                item.status, item.media_file_id = "duplicate", known_sha[item.sha256]
                continue
            request_id = str(uuid.uuid4())
            by_request_id[request_id] = item
            request_files.append(
                ImportFile(
                    id=request_id,
                    rootId=None,
                    path=[],
                    name=path.name,
                    mimeType=mime_type(path),
                    byteLength=item.byte_length,
                    lastModified=int(path.stat().st_mtime * 1000),
                    sha256=item.sha256,
                    kind=item.kind,  # type: ignore[arg-type]
                )
            )
        if not request_files:
            return report

        # ── phase 1: reserve ──
        revision = await self.workspace_revision(session_id)
        request = ImportRequest(
            sessionId=session_id, batchId=report.batch_id, expectedRevision=revision, files=request_files
        )
        await progress(f"reserving {len(request_files)} file(s)")
        manifest = ImportManifest.model_validate(
            await self._http.post_json("/api/media/imports", request.model_dump())
        )
        for rejected in manifest.rejected:
            item = by_request_id.get(str(rejected.get("id")))
            if item:
                item.status, item.reason = "rejected", str(rejected.get("reason"))
        for duplicate in manifest.duplicates:
            item = by_request_id.get(str(duplicate.get("id")))
            if item:
                item.status, item.media_file_id = "duplicate", str(duplicate.get("existingMediaId"))
        accepted = {f.id: f for f in manifest.files}
        pending: list[tuple[ImportedItem, ImportFile]] = []
        for req in request_files:
            item = by_request_id[req.id]
            if item.status != "pending":
                continue
            if req.id not in accepted:
                item.status, item.reason = "rejected", "not_in_manifest"
                continue
            item.media_file_id = req.id
            pending.append((item, req))

        # ── phase 2: upload each accepted file ──
        for index, (item, req) in enumerate(pending, start=1):
            await progress(f"uploading {item.name} ({index}/{len(pending)})", index / (len(pending) + 1))
            try:
                await self._upload_one(session_id, report.batch_id, item, req)
            except CassetteError as exc:
                item.status, item.reason = "failed", f"{exc.code}: {exc.message}"

        # ── phase 3: readiness ──
        uploaded = [i for i in report.items if i.status == "uploaded" and i.media_file_id]
        if uploaded:
            await progress("waiting for the backend to process the media")
            await self._wait_ready(uploaded, timeout_sec=ready_timeout_sec, on_progress=progress)
        return report

    async def _upload_one(self, session_id: str, batch_id: str, item: ImportedItem, req: ImportFile) -> None:
        source = Path(item.path)
        artifacts = [
            UploadArtifactRequest(
                role="original", sha256=req.sha256, byteLength=req.byteLength, mimeType=req.mimeType
            )
        ]
        local_paths: dict[str, Path] = {"original": source}
        preparation: dict[str, Any] | None = None
        workdir: Path | None = None
        try:
            if req.kind == "video":
                workdir = Path(tempfile.mkdtemp(prefix="omc-prep-"))
                prepared = await self._tools.prepare_video(source, workdir)
                preparation = prepared["report"]
                for role in ("canonical", "preview"):
                    path = prepared["paths"][role]
                    local_paths[role] = path
                    artifacts.append(
                        UploadArtifactRequest(
                            role=role,
                            sha256=await sha256_file_async(path),
                            byteLength=path.stat().st_size,
                            mimeType="video/mp4",
                        )  # type: ignore[arg-type]
                    )
            upload_id = str(uuid.uuid4())
            request = UploadRequest(
                sessionId=session_id,
                batchId=batch_id,
                mediaId=req.id,
                uploadId=upload_id,
                artifacts=artifacts,
                preparation=preparation,
            )
            try:
                registration = UploadRegistration.model_validate(
                    await self._http.post_json("/api/media/imports/upload", request.model_dump())
                )
            except HttpError as exc:
                if req.kind == "video" and exc.status == 400 and exc.code == "invalid_request":
                    raise CassetteError(
                        "video_import_requires_backend_update",
                        "this Cassette backend only accepts browser-prepared video (mediabunny). "
                        "Audio and images import fine; video needs the backend change described in docs/v2/backend-changes.md.",
                        details=exc.details,
                    ) from exc
                raise
            item.upload_id = registration.upload.id
            for target in registration.artifacts:
                await self._http.put_file(
                    target.uploadUrl, local_paths[target.role], target.uploadContentType
                )
            completed = RegisteredUpload.model_validate(
                (
                    await self._http.post_json(
                        "/api/media/imports/upload/complete", {"sessionId": session_id, "uploadId": upload_id}
                    )
                )
                or {}
            )
            if completed.status in ("failed", "cancelled"):
                raise CassetteError("upload_rejected", f"upload {completed.status}: {completed.error}")
            item.status = "uploaded"
        finally:
            if workdir is not None:
                shutil.rmtree(workdir, ignore_errors=True)

    async def _wait_ready(
        self, items: list[ImportedItem], *, timeout_sec: float, on_progress: ProgressCallback
    ) -> None:
        deadline = time.monotonic() + timeout_sec
        by_id = {i.media_file_id: i for i in items if i.media_file_id}
        last_report = 0.0
        delay = 1.0
        while by_id and time.monotonic() < deadline:
            statuses = await self.statuses(list(by_id))
            for status in statuses:
                item = by_id.get(status.mediaFileId)
                if item is None:
                    continue
                item.readiness = status.readinessPhase or status.uploadStatus
                item.ai_ready = bool(status.aiReady or status.analysisReady)
                if status.failed:
                    item.status, item.reason = "failed", status.errorMessage or "processing failed"
                    by_id.pop(status.mediaFileId, None)
                elif status.playbackReady or status.fullyReady:
                    # playbackReady = the timeline can use it now; AI analysis may still be running.
                    item.status = "ready"
                    by_id.pop(status.mediaFileId, None)
            if not by_id:
                break
            if time.monotonic() - last_report > 20:
                last_report = time.monotonic()
                await on_progress(f"processing… {len(items) - len(by_id)}/{len(items)} ready", None)
            await asyncio.sleep(delay)
            delay = min(5.0, delay * 1.5)
        for item in by_id.values():
            if item.status == "uploaded":
                item.status, item.reason = "failed", "readiness_timeout"
