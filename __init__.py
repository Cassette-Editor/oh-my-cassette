from __future__ import annotations

from pathlib import Path

if __package__:
    from . import schemas, tools
else:  # Pytest may import this file as a bare module when the repo root is not named "cassette".
    schemas = None
    tools = None


def register(ctx) -> None:
    if schemas is None or tools is None:
        raise RuntimeError("cassette plugin must be loaded as a package directory")

    plugin_tools = (
        ("cassette_ingest_media", schemas.CASSETTE_INGEST_MEDIA, tools.cassette_ingest_media),
        ("cassette_list_assets", schemas.CASSETTE_LIST_ASSETS, tools.cassette_list_assets),
        ("cassette_make_prompt", schemas.CASSETTE_MAKE_PROMPT, tools.cassette_make_prompt),
        ("cassette_match_bgm", schemas.CASSETTE_MATCH_BGM, tools.cassette_match_bgm),
        ("cassette_match_exact_bgm", schemas.CASSETTE_MATCH_EXACT_BGM, tools.cassette_match_exact_bgm),
        ("jamendo_music_matcher", schemas.JAMENDO_MUSIC_MATCHER, tools.jamendo_music_matcher),
        ("cassette_answer_question", schemas.CASSETTE_ANSWER_QUESTION, tools.cassette_answer_question),
        ("cassette_run_job", schemas.CASSETTE_RUN_JOB, tools.cassette_run_job),
        ("cassette_job_status", schemas.CASSETTE_JOB_STATUS, tools.cassette_job_status),
        ("cassette_review_completion", schemas.CASSETTE_REVIEW_COMPLETION, tools.cassette_review_completion),
        ("cassette_cancel_job", schemas.CASSETTE_CANCEL_JOB, tools.cassette_cancel_job),
    )

    for name, schema, handler in plugin_tools:
        ctx.register_tool(
            name=name,
            toolset="cassette",
            schema=schema,
            handler=handler,
        )

    ctx.register_command(
        "cassette",
        handler=tools.handle_cassette_command,
        description="Cassette video-editing automation status and cancellation",
        args_hint="help|status <job_id>|cancel <job_id>|cut [job_id]|language [zh|en]|recent [limit]",
    )
    ctx.register_command(
        "cut",
        handler=tools.handle_cut_command,
        description="Pause the active Cassette browser operation without closing the live session",
        args_hint="[job_id]",
    )
    ctx.register_command(
        "cassette_model",
        handler=tools.handle_cassette_model_command,
        description="Choose the Cassette model for the current QQ/Telegram gateway session",
        args_hint="",
    )
    ctx.register_hook("pre_gateway_dispatch", tools.ingest_gateway_media)
    ctx.register_hook("pre_llm_call", tools.inject_cassette_context)
    ctx.register_hook("post_tool_call", tools.log_cassette_tool_call)
    ctx.register_hook("on_session_finalize", tools.close_cassette_browser_sessions)
    ctx.register_hook("on_session_reset", tools.close_cassette_browser_sessions)

    # Keep the gateway-specific Hermes workflow out of the native Codex/Claude plugin skill path.
    skill_path = Path(__file__).parent / "hermes" / "skills" / "cassette-video-edit" / "SKILL.md"
    if skill_path.exists():
        ctx.register_skill(
            "cassette-video-edit",
            skill_path,
            "Orchestrate QQ/Telegram gateway media into Cassette video editing jobs.",
        )
