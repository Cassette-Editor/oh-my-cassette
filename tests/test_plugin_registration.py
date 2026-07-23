from __future__ import annotations

from cassette import register


class FakeContext:
    def __init__(self):
        self.tools = []
        self.commands = []
        self.hooks = []
        self.skills = []

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands.append({"name": name, "handler": handler, "description": description, "args_hint": args_hint})

    def register_hook(self, hook_name, callback):
        self.hooks.append((hook_name, callback))

    def register_skill(self, name, path, description=""):
        self.skills.append({"name": name, "path": path, "description": description})


def test_plugin_registers_tools_command_hooks_and_skill():
    ctx = FakeContext()
    register(ctx)

    assert {tool["name"] for tool in ctx.tools} == {
        "cassette_ingest_media",
        "cassette_list_assets",
        "cassette_make_prompt",
        "cassette_match_bgm",
        "cassette_match_exact_bgm",
        "jamendo_music_matcher",
        "cassette_answer_question",
        "cassette_run_job",
        "cassette_job_status",
        "cassette_review_completion",
        "cassette_cancel_job",
        "cassette_timeline",
        "cassette_edit",
    }
    assert {command["name"] for command in ctx.commands} == {"cassette", "cut", "cassette_model"}
    assert next(command for command in ctx.commands if command["name"] == "cassette")["args_hint"] == (
        "help|status <job_id>|cancel <job_id>|cut [job_id]|language [zh|en]|recent [limit]"
    )
    assert {name for name, _ in ctx.hooks} == {
        "pre_gateway_dispatch",
        "pre_llm_call",
        "post_tool_call",
        "on_session_finalize",
        "on_session_reset",
    }
    assert all(tool.get("check_fn") is None for tool in ctx.tools)
    assert ctx.skills[0]["name"] == "cassette-video-edit"
    assert ctx.skills[0]["path"].exists()
