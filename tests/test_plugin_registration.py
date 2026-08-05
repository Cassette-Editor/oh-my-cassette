from __future__ import annotations

import json
from pathlib import Path

from cassette import register


ROOT = Path(__file__).resolve().parents[1]


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


def test_plugin_registers_gateway_shim_and_shared_mcp_skill():
    ctx = FakeContext()
    register(ctx)

    # Hermes gets the same tools as every other host from the stdio MCP server.
    # The native plugin is deliberately only the gateway lifecycle shim.
    assert ctx.tools == []
    assert {command["name"] for command in ctx.commands} == {"cassette", "cut", "cassette_model"}
    assert next(command for command in ctx.commands if command["name"] == "cassette")["args_hint"] == (
        "help|status <job_id>|cancel <job_id>|cut [job_id]|language [zh|en]|recent [limit]"
    )
    assert {name for name, _ in ctx.hooks} == {
        "pre_gateway_dispatch",
        "pre_llm_call",
        "pre_tool_call",
        "post_tool_call",
        "on_session_finalize",
        "on_session_reset",
    }
    assert {skill["name"] for skill in ctx.skills} == {
        "cassette-video-edit",
        "cassette-gateway-video-edit",
    }
    shared = next(skill for skill in ctx.skills if skill["name"] == "cassette-video-edit")
    assert shared["path"] == ROOT / "skills" / "cassette-video-edit" / "SKILL.md"


def test_login_refuses_to_write_under_the_hermes_adapter(tmp_path, monkeypatch):
    # Parity is the tool *name*, not the behaviour. Hermes resolves credentials from its
    # process env and ~/.hermes/.env, and the stored file is only read under the mcp adapter,
    # so writing one here would create a file nothing ever reads.
    from cassette.core import tools

    monkeypatch.setenv("CASSETTE_CONFIG_HOME", str(tmp_path / "config"))
    envelope = json.loads(tools.cassette_login({"email": "person@example.test", "password": "pasted"}))

    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "auth_unsupported_adapter"
    assert ".hermes/.env" in envelope["error"]["message"]
    assert envelope["error"]["recoverable"] is False
    assert not (tmp_path / "config").exists()
    assert "pasted" not in json.dumps(envelope)
