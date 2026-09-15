from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations

from oh_my_cassette.app import App
from oh_my_cassette.cassette.errors import CassetteError
from oh_my_cassette.tools.common import Progress, guarded, project_fields


def register(server: MCPServer, app: App) -> None:
    @server.tool(
        name="cassette_import",
        title="Import local media into the project",
        description=(
            "Upload local video/audio/image files into the project's media library so the Cassette agent can use "
            "them. Give absolute paths (or paths relative to the workspace). Video is transcoded locally with ffmpeg "
            "before upload; audio and images upload as-is. Files already imported (same content) are reported as "
            "duplicate, not re-uploaded. The call streams progress and returns when the backend has finished "
            "analysing the media (ready), so the next cassette_run can reference it."
        ),
        annotations=ToolAnnotations(title="Import media", read_only_hint=False, open_world_hint=True),
    )
    @guarded
    async def cassette_import(
        paths: list[str], ctx: Context, project_id: str | None = None
    ) -> dict[str, Any]:
        if not paths:
            raise CassetteError("bad_request", "paths must contain at least one file")
        project = await app.resolve(project_id)
        record = project.record
        resolved = [
            Path(p).expanduser() if Path(p).expanduser().is_absolute() else Path(app.cwd) / p for p in paths
        ]
        report = await app.media.import_paths(
            project.project_id,
            resolved,
            on_progress=Progress(ctx),
            ready_timeout_sec=app.settings.import_ready_timeout_sec,
            known_sha=record.imports,
        )
        media_map = dict(record.media)
        import_map = dict(record.imports)
        for item in report.items:
            if item.media_file_id and item.status in ("ready", "duplicate", "uploaded"):
                media_map[item.media_file_id] = item.path
                if item.sha256:
                    import_map[item.sha256] = item.media_file_id
        app.state.update_project(project.project_id, media=media_map, imports=import_map)
        names = await app.media_names(project.project_id)
        items = [
            {
                "path": i.path,
                "name": i.name,
                "kind": i.kind,
                "media_file_id": i.media_file_id,
                "status": i.status,
                "reason": i.reason,
                "readiness": i.readiness,
                "ai_ready": i.ai_ready,
            }
            for i in report.items
        ]
        ready = [i for i in report.items if i.status in ("ready", "duplicate")]
        status = "ok" if ready and len(ready) == len(report.items) else ("partial" if ready else "failed")
        return {
            "status": status,
            **project_fields(app, project),
            "batch_id": report.batch_id,
            "imported": len(ready),
            "items": items,
            "library": [{"media_file_id": mid, "name": name} for mid, name in names.items()],
            "next": "call cassette_run with the user's editing request; mention files by name.",
        }
