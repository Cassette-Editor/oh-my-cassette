from __future__ import annotations

import time
from typing import Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations

from oh_my_cassette.app import App
from oh_my_cassette.cassette.errors import CassetteError
from oh_my_cassette.render.contact_sheet import build_contact_sheet
from oh_my_cassette.render.digest import timeline_delta, timeline_digest
from oh_my_cassette.tools.common import guarded, project_fields


def register(server: MCPServer, app: App) -> None:
    @server.tool(
        name="cassette_timeline",
        title="Read the current timeline",
        description=(
            "Ground every statement about the project in this: returns the current version, a bounded per-track "
            "digest of the active sequence, the media library, and optionally a contact sheet (contact_sheet=true) "
            "built locally from imported originals. Read-only."
        ),
        annotations=ToolAnnotations(
            title="Timeline", read_only_hint=True, idempotent_hint=True, open_world_hint=True
        ),
    )
    @guarded
    async def cassette_timeline(
        ctx: Context, project_id: str | None = None, contact_sheet: bool = False
    ) -> dict[str, Any]:
        project = await app.resolve(project_id)
        document = project.document
        names = await app.media_names(project.project_id)
        body: dict[str, Any] = {
            "status": "ok",
            **project_fields(app, project),
            "timeline": timeline_digest(document, names),
            "library": [
                {
                    "media_file_id": mid,
                    "name": name,
                    "local_path": app.local_media(project.project_id).get(mid),
                }
                for mid, name in names.items()
            ],
        }
        if contact_sheet:
            dest = (
                app.artifacts_dir(project.project_id)
                / f"contact-sheet-v{project.snapshot.version}-{int(time.time())}.jpg"
            )
            try:
                body["contact_sheet"] = await build_contact_sheet(
                    document, app.local_media(project.project_id), dest, ffmpeg=app.settings.ffmpeg
                )
            except CassetteError as exc:
                body["contact_sheet"] = {"error": exc.to_dict()}
        return body

    @server.tool(
        name="cassette_history",
        title="Undo, redo, or list timeline history",
        description=(
            "action=list shows the commit history (who/what/when, versions). action=undo / action=redo move the "
            "history cursor by one step; action=restore_before with group_id rewinds to just before that commit. "
            "Refuses to move while an agent run holds the timeline. Returns the resulting version and delta."
        ),
        annotations=ToolAnnotations(title="History", read_only_hint=False, open_world_hint=True),
    )
    @guarded
    async def cassette_history(
        ctx: Context,
        action: Literal["list", "undo", "redo", "restore_before"] = "list",
        project_id: str | None = None,
        group_id: str | None = None,
    ) -> dict[str, Any]:
        project = await app.resolve(project_id)
        history = await app.projects.history(project.project_id)
        groups = [
            {
                "group_id": g.groupId,
                "cursor": g.cursorSequence,
                "label": g.label,
                "source": g.source,
                "version_before": g.versionBefore,
                "version_after": g.versionAfter,
                "committed_at": g.committedAt,
                "run_id": g.agentRunId,
                "applied": g.cursorSequence <= history.cursorSequence,
            }
            for g in history.groups
        ]
        if action == "list":
            return {
                "status": "ok",
                **project_fields(app, project),
                "cursor": history.cursorSequence,
                "groups": groups[-30:],
                "total_groups": len(groups),
            }
        command: dict[str, Any] = {"kind": action}
        if action == "restore_before":
            if not group_id:
                raise CassetteError("bad_request", "restore_before needs group_id")
            command["groupId"] = group_id
        preview = await app.projects.history_preview(project.project_id, command)
        if preview.status != "ready":
            return {
                "status": "rejected",
                **project_fields(app, project),
                "reason": preview.reason or preview.status,
                "cursor": history.cursorSequence,
            }
        before = project.document
        outcome = await app.projects.history_commit(
            project.project_id,
            command,
            expected_version=history.version,
            expected_cursor=history.cursorSequence,
        )
        if outcome.status not in ("moved", "idempotent_existing"):
            return {
                "status": "rejected",
                **project_fields(app, project),
                "reason": outcome.reason or outcome.status,
                "details": outcome.model_dump(exclude={"document"}),
            }
        after = (await app.refresh_snapshot(project.project_id)).document
        delta = timeline_delta(before, after)
        return {
            "status": "ok",
            "action": action,
            "project_id": project.project_id,
            "editor_url": app.editor_url(project.project_id),
            "version": int(after.get("version", 0)),
            "version_from": delta.version_from,
            "version_to": delta.version_to,
            "timeline_delta": delta.summary(before, after),
            "timeline": timeline_digest(after, await app.media_names(project.project_id)),
        }
