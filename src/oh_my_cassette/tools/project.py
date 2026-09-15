from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from oh_my_cassette.app import App
from oh_my_cassette.cassette.errors import CassetteError
from oh_my_cassette.tools.common import guarded, project_fields


def register(server: MCPServer, app: App) -> None:
    @server.tool(
        name="cassette_project",
        title="Open or create a Cassette project",
        description=(
            "Bind this workspace to a Cassette project. action=open (default) reuses the project bound to the "
            "current directory, or the most recent one; action=create makes a new empty project; action=list shows "
            "known projects; action=forget unbinds one locally. Every other tool accepts an optional project_id and "
            "otherwise uses the bound project. Returns project_id, editor_url (open it in a browser to watch the "
            "edit live) and the current timeline version."
        ),
        annotations=ToolAnnotations(
            title="Cassette project", read_only_hint=False, idempotent_hint=True, open_world_hint=True
        ),
    )
    @guarded
    async def cassette_project(
        action: Literal["open", "create", "list", "forget"] = "open",
        project_id: str | None = None,
        title: str | None = None,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        if action == "list":
            records = app.state.list_projects()
            return {
                "status": "ok",
                "projects": [
                    {
                        "project_id": r.project_id,
                        "title": r.title,
                        "editor_url": app.editor_url(r.project_id),
                        "last_used_at": r.last_used_at,
                        "media_count": len(r.media),
                    }
                    for r in records
                ],
                "bound_project_id": (
                    app.state.project_for_cwd(cwd or app.cwd) or records[0] if records else None
                )
                and (app.state.project_for_cwd(cwd or app.cwd) or records[0]).project_id,
            }
        if action == "forget":
            if not project_id:
                raise CassetteError("bad_request", "action=forget needs project_id")
            app.state.forget_project(project_id)
            return {"status": "ok", "forgotten": project_id}
        if action == "create":
            project = await app.create_project(title, cwd=cwd)
        else:
            project = (
                await app.open_project(project_id, cwd=cwd)
                if project_id
                else await app.resolve(None, cwd=cwd)
            )
        media = await app.media_names(project.project_id)
        return {
            "status": "ok",
            "action": action,
            **project_fields(app, project),
            "title": project.record.title,
            "chat_session_id": project.chat_session_id,
            "anonymous": project.record.anonymous,
            "media_count": len(media),
            "backend": app.settings.api_url,
        }
