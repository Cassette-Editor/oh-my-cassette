from __future__ import annotations

from pathlib import Path

if __package__:
    from .core import gateway
else:  # Pytest may import this file as a bare module when the repo root is not named "cassette".
    gateway = None


def register(ctx) -> None:
    if gateway is None:
        raise RuntimeError("cassette plugin must be loaded as a package directory")

    # Tools intentionally come from the same stdio MCP server used by every host. The native
    # Hermes plugin owns only gateway lifecycle behavior; a second set of direct handlers would
    # create two subtly different Cassette runtimes.

    ctx.register_command(
        "cassette",
        handler=gateway.handle_cassette_command,
        description="Cassette video-editing automation status and cancellation",
        args_hint="help|status <job_id>|cancel <job_id>|cut [job_id]|language [zh|en]|recent [limit]",
    )
    ctx.register_command(
        "cut",
        handler=gateway.handle_cut_command,
        description="Pause the active Cassette operation without ending the session",
        args_hint="[job_id]",
    )
    ctx.register_command(
        "cassette_model",
        handler=gateway.handle_cassette_model_command,
        description="Choose the Cassette model for the current QQ/Telegram gateway session",
        args_hint="",
    )
    ctx.register_hook("pre_gateway_dispatch", gateway.ingest_gateway_media)
    ctx.register_hook("pre_llm_call", gateway.inject_cassette_context)
    ctx.register_hook("pre_tool_call", gateway.guard_cassette_run_job_call)
    ctx.register_hook("post_tool_call", gateway.log_cassette_tool_call)
    ctx.register_hook("on_session_finalize", gateway.close_cassette_sessions)
    ctx.register_hook("on_session_reset", gateway.close_cassette_sessions)

    root = Path(__file__).parent
    shared_skill = root / "skills" / "cassette-video-edit" / "SKILL.md"
    if shared_skill.exists():
        ctx.register_skill(
            "cassette-video-edit",
            shared_skill,
            "Edit and export media through the shared Cassette MCP workflow.",
        )

    gateway_skill = root / "hermes" / "skills" / "cassette-video-edit" / "SKILL.md"
    if gateway_skill.exists():
        ctx.register_skill(
            "cassette-gateway-video-edit",
            gateway_skill,
            "Add QQ/Telegram gateway delivery behavior to the shared Cassette MCP workflow.",
        )
