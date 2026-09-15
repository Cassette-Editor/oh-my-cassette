from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations

from oh_my_cassette.app import App
from oh_my_cassette.cassette.errors import CassetteError
from oh_my_cassette.cassette.export import build_manifest
from oh_my_cassette.cassette.models import ExportJob
from oh_my_cassette.tools.common import Progress, guarded, project_fields

TRANSIENT_STORAGE_ERRORS = ("object_write_unavailable", "object_write_pending")


def _transient_storage_failure(job: ExportJob) -> bool:
    text = f"{job.errorCode or ''} {job.error or ''}"
    return job.status == "error" and any(code in text for code in TRANSIENT_STORAGE_ERRORS)


def register(server: MCPServer, app: App) -> None:
    @server.tool(
        name="cassette_export",
        title="Render and download the timeline",
        description=(
            "Render the active sequence to an MP4 on the Cassette backend and download it. Only call this when the "
            "user explicitly asks to export/render/finish; editing turns never render. output_dir defaults to "
            "./exports under the workspace. Returns the local file path, size and the job id."
        ),
        annotations=ToolAnnotations(title="Export video", read_only_hint=False, open_world_hint=True),
    )
    @guarded
    async def cassette_export(
        ctx: Context,
        project_id: str | None = None,
        file_name: str | None = None,
        output_dir: str | None = None,
    ) -> dict[str, Any]:
        project = await app.resolve(project_id)
        progress = Progress(ctx)
        name = file_name or f"cassette-{project.project_id[-8:]}-v{project.snapshot.version}.mp4"
        manifest = build_manifest(project.project_id, project.document, name)
        await progress("creating export job")
        job = await app.export.create_job(manifest)
        final = await app.export.wait(
            job.jobId, timeout_sec=app.settings.export_timeout_sec, on_progress=progress
        )
        if final.status != "done" and _transient_storage_failure(final):
            # The backend occasionally refuses its own object reservation on a fresh project
            # (docs/v2/backend-changes.md, P2); a second job goes through.
            await progress("storage reservation refused; creating the export job again")
            job = await app.export.create_job(manifest)
            final = await app.export.wait(
                job.jobId, timeout_sec=app.settings.export_timeout_sec, on_progress=progress
            )
        if final.status != "done":
            return {
                "status": "failed",
                **project_fields(app, project),
                "job_id": final.jobId,
                "error": {
                    "code": final.errorCode or f"export_{final.status}",
                    "message": final.error or final.status,
                    "retryable": False,
                },
            }
        target_dir = Path(output_dir).expanduser() if output_dir else Path(app.cwd) / "exports"
        if not target_dir.is_absolute():
            target_dir = Path(app.cwd) / target_dir
        dest = target_dir / manifest["outputFileName"]
        await progress("downloading")
        started = time.monotonic()
        await app.export.download(final, dest)
        size = dest.stat().st_size
        if size == 0:
            raise CassetteError("export_empty", "downloaded file is empty")
        return {
            "status": "ok",
            **project_fields(app, project),
            "job_id": final.jobId,
            "file": str(dest),
            "bytes": size,
            "download_sec": round(time.monotonic() - started, 1),
            "total_frames": manifest["totalFrames"],
            "resolution": f"{manifest['width']}x{manifest['height']}",
        }
