"""cassette_edit direct-lane tests: flag gate, freshness check, job-active guard, wire format."""

from __future__ import annotations

import json

import pytest

from cassette import jobs, tools
from tests.test_api_transport_mock import _MockCassetteAPI, _serve

_DOC = {
    "schemaVersion": 2,
    "projectId": "try-session-ed",
    "version": 5,
    "sequenceTimebase": {"num": 30, "den": 1},
    "fps": 30,
    "compositionWidth": 1920,
    "compositionHeight": 1080,
    "entities": {
        "tracks": {"t1": {"id": "t1", "name": "V", "type": "video"}},
        "clips": {
            "c1": {
                "id": "c1",
                "name": "intro.mp4",
                "type": "video",
                "trackId": "t1",
                "startFrame": 0,
                "durationInFrames": 90,
            }
        },
        "transitions": {},
    },
    "order": {"trackIds": ["t1"], "clipIds": ["c1"], "transitionIds": []},
}


class _CommandsAPI(_MockCassetteAPI):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/projects/"):
            self.rec["requests"].append(("GET", path))
            return self._json(200, {"document": json.loads(json.dumps(_DOC))})
        return super().do_GET()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path.endswith("/commands") and path.startswith("/api/projects/"):
            body = self._body()
            self.rec["command_envelopes"] = self.rec.get("command_envelopes", []) + [body]
            after = json.loads(json.dumps(_DOC))
            after["version"] = 6
            after["entities"]["clips"]["c1"]["durationInFrames"] = 60
            return self._json(
                200,
                {
                    "type": "project_operation_committed",
                    "projectId": "try-session-ed",
                    "commandId": body.get("commandId"),
                    "versionBefore": 5,
                    "versionAfter": 6,
                    "document": after,
                    "diff": {"beforeVersion": 5, "afterVersion": 6, "entries": [], "entryCount": 0},
                },
            )
        return super().do_POST()


@pytest.fixture
def commands_api(monkeypatch):
    server = _serve(_CommandsAPI, monkeypatch)
    monkeypatch.setenv("CASSETTE_DIRECT_EDIT", "1")
    yield server
    server.shutdown()
    server.server_close()


def test_edit_disabled_without_flag(cassette_env, monkeypatch):
    monkeypatch.delenv("CASSETTE_DIRECT_EDIT", raising=False)
    result = json.loads(tools.cassette_edit({"session_id": "try-session-ed", "tool_name": "timeline_trim"}))
    assert result["ok"] is False
    assert result["error"]["code"] == "direct_edit_disabled"


def test_edit_posts_agent_tool_envelope_and_returns_delta(cassette_env, commands_api):
    result = json.loads(
        tools.cassette_edit(
            {
                "session_id": "try-session-ed",
                "tool_name": "timeline_trim",
                "input": {"clipId": "c1", "durationInFrames": 60},
                "expected_version": 5,
            }
        )
    )
    assert result["ok"], result
    assert result["data"]["version_before"] == 5
    assert result["data"]["version_after"] == 6
    assert "~ V1/A intro.mp4" in result["data"]["delta"]
    assert result["data"]["ctl"].startswith("TIMELINE try-session-ed v6")
    envelope = commands_api.rec["command_envelopes"][0]
    assert envelope["source"] == "agent"
    assert envelope["toolName"] == "timeline_trim"
    assert envelope["command"] == {
        "type": "agent-tool",
        "toolName": "timeline_trim",
        "input": {"clipId": "c1", "durationInFrames": 60},
    }
    assert envelope["commandId"]


def test_edit_stale_version_refuses_with_fresh_ctl(cassette_env, commands_api):
    result = json.loads(
        tools.cassette_edit(
            {
                "session_id": "try-session-ed",
                "tool_name": "timeline_trim",
                "input": {"clipId": "c1"},
                "expected_version": 3,
            }
        )
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "stale_timeline"
    assert result["error"]["details"]["ctl"].startswith("TIMELINE try-session-ed v5")
    assert commands_api.rec.get("command_envelopes") is None  # nothing was posted


def test_edit_refuses_while_job_holds_session(cassette_env, commands_api):
    job = jobs.create_job("ed", "prompt", None, [], {"cassette_session_id": "try-session-ed"})
    jobs.update_job(job["job_id"], status="running")
    result = json.loads(
        tools.cassette_edit(
            {"session_id": "try-session-ed", "tool_name": "timeline_trim", "input": {"clipId": "c1"}}
        )
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "job_active"
    assert job["job_id"] in result["error"]["message"]


def test_edit_undo_maps_to_history_cursor(cassette_env, commands_api):
    result = json.loads(
        tools.cassette_edit(
            {"session_id": "try-session-ed", "tool_name": "undo", "input": {"cursorSequence": 4}}
        )
    )
    assert result["ok"], result
    envelope = commands_api.rec["command_envelopes"][0]
    assert envelope["command"] == {"type": "set-operation-history-cursor", "cursorSequence": 4}
    assert "toolName" not in envelope


def test_edit_unknown_tool_lists_catalog(cassette_env, commands_api):
    result = json.loads(
        tools.cassette_edit({"session_id": "try-session-ed", "tool_name": "timeline_nuke", "input": {}})
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "unknown_tool"
    assert "timeline_trim" in result["error"]["details"]["tools"]


def test_edit_surfaces_server_validation_error(cassette_env, monkeypatch):
    class _RejectingAPI(_CommandsAPI):
        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path.endswith("/commands"):
                self._body()
                return self._json(
                    200,
                    {"ok": False, "code": "VALIDATION_FAILED", "message": "input failed timeline_trim static validation"},
                )
            return super().do_POST()

    server = _serve(_RejectingAPI, monkeypatch)
    monkeypatch.setenv("CASSETTE_DIRECT_EDIT", "1")
    try:
        result = json.loads(
            tools.cassette_edit(
                {"session_id": "try-session-ed", "tool_name": "timeline_trim", "input": {"bogus": 1}}
            )
        )
        assert result["ok"] is False
        assert result["error"]["code"] == "validation_failed"
        assert "static validation" in result["error"]["message"]
    finally:
        server.shutdown()
        server.server_close()
