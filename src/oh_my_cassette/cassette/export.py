"""Export: project the document into a render manifest, create the job, poll, download."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from oh_my_cassette.cassette.errors import CassetteError
from oh_my_cassette.cassette.http import CassetteHttp
from oh_my_cassette.cassette.models import ExportJob
from oh_my_cassette.render.digest import sequence_arrays, total_frames

ProgressCallback = Callable[[str, float | None], Awaitable[None]]
TERMINAL = {"done", "error", "cancelled"}


def _normalize_main_track(tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if any(t.get("isMain") for t in tracks):
        return tracks
    out = [dict(t) for t in tracks]
    for track in out:
        if track.get("type") == "video":
            track["isMain"] = True
            break
    return out


def project_sequence(document: dict[str, Any], sequence_id: str) -> dict[str, Any]:
    """Port of packages/shared/src/timeline/projection.ts `projectSequence` (single sequence)."""
    sequences = document.get("entities", {}).get("sequences", {})
    seq = sequences.get(sequence_id)
    if not seq:
        raise CassetteError("sequence_missing", f"sequence {sequence_id} is not in the document")
    arrays = sequence_arrays(document, sequence_id)
    clips = []
    for clip in arrays["clips"]:
        if clip.get("type") == "nested-sequence":
            child = sequences.get((clip.get("source") or {}).get("sequenceId"))
            if not child:
                raise CassetteError("sequence_missing", "nested sequence source is unavailable")
            clip = {
                **clip,
                "visual": {
                    **(clip.get("visual") or {}),
                    "sourceWidth": child["compositionWidth"],
                    "sourceHeight": child["compositionHeight"],
                },
            }
        clips.append(clip)
    return {
        "sequenceId": sequence_id,
        "tracks": _normalize_main_track(arrays["tracks"]),
        "clips": clips,
        "transitions": arrays["transitions"],
        "sequenceTimebase": dict(seq["timebase"]),
        "compositionWidth": seq["compositionWidth"],
        "compositionHeight": seq["compositionHeight"],
        "totalFrames": total_frames(clips),
        "semanticAnnotationOwners": {},
        "semanticAnnotationIds": list(seq.get("semanticAnnotationIds", [])),
    }


def render_bundle(document: dict[str, Any]) -> dict[str, Any]:
    root = document["activeSequenceId"]
    sequences: dict[str, Any] = {}
    visiting: set[str] = set()

    def visit(sequence_id: str, depth: int) -> None:
        if depth > 32:
            raise CassetteError("nesting_too_deep", "timeline render nesting exceeds 32")
        if sequence_id in visiting:
            raise CassetteError("sequence_cycle", f"sequence cycle at {sequence_id}")
        if sequence_id in sequences:
            return
        visiting.add(sequence_id)
        projection = project_sequence(document, sequence_id)
        sequences[sequence_id] = projection
        for clip in projection["clips"]:
            if clip.get("type") == "nested-sequence":
                visit(clip["source"]["sequenceId"], depth + 1)
        visiting.discard(sequence_id)

    visit(root, 0)
    return {"rootSequenceId": root, "sequences": sequences}


def sanitize_file_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip()).strip("._") or "export"
    return cleaned if cleaned.lower().endswith(".mp4") else f"{cleaned}.mp4"


def build_manifest(project_id: str, document: dict[str, Any], output_file_name: str) -> dict[str, Any]:
    bundle = render_bundle(document)
    root = bundle["sequences"][bundle["rootSequenceId"]]
    if root["totalFrames"] < 1:
        raise CassetteError("timeline_empty", "the active sequence has no clips to export")
    return {
        "projectId": project_id,
        "clips": root["clips"],
        "tracks": root["tracks"],
        "transitions": root["transitions"],
        "sequenceTimebase": root["sequenceTimebase"],
        "totalFrames": root["totalFrames"],
        "width": root["compositionWidth"],
        "height": root["compositionHeight"],
        "outputFileName": sanitize_file_name(output_file_name),
        "blobAssets": [],
        "renderBundle": bundle,
    }


class ExportClient:
    def __init__(self, http: CassetteHttp) -> None:
        self._http = http

    async def create_job(self, manifest: dict[str, Any]) -> ExportJob:
        body = await self._http.request(
            "POST",
            "/api/export/jobs",
            # A plain text part: @fastify/multipart parses JSON-typed fields into objects, which the
            # route then rejects as an invalid field.
            files=[("manifest", (None, json.dumps(manifest).encode("utf-8")))],
            ok=(200, 201, 202),
            timeout=300,
            retries=0,
        )
        return ExportJob.model_validate(body.json())

    async def job(self, job_id: str) -> ExportJob:
        return ExportJob.model_validate(await self._http.get_json(f"/api/export/jobs/{job_id}"))

    async def cancel(self, job_id: str) -> None:
        await self._http.request("POST", f"/api/export/jobs/{job_id}/cancel", ok=(200, 202, 204, 409))

    async def wait(
        self, job_id: str, *, timeout_sec: float, on_progress: ProgressCallback | None = None
    ) -> ExportJob:
        deadline = time.monotonic() + timeout_sec
        delay = 1.0
        last: ExportJob | None = None
        last_percent = -1
        while time.monotonic() < deadline:
            last = await self.job(job_id)
            if last.status in TERMINAL:
                return last
            if on_progress is not None and last.progressPercent != last_percent:
                last_percent = last.progressPercent
                await on_progress(
                    f"{last.step or last.status} {last.progressPercent}%", last.progressPercent / 100
                )
            await asyncio.sleep(delay)
            delay = min(4.0, delay * 1.3)
        raise CassetteError(
            "export_timeout",
            f"export job {job_id} did not finish within {int(timeout_sec)}s",
            details=last.model_dump() if last else None,
        )

    async def download(self, job: ExportJob, dest: Path) -> Path:
        return await self._http.download(job.fileUrl or f"/api/export/jobs/{job.jobId}/file", dest)
