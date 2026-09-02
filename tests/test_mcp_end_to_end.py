"""The full documented flow through the real MCP process against a real local Cassette API.

Every other test covers one seam: the transport alone, or the protocol alone. Nothing drove a
host's actual sequence -- ingest, converse, read the timeline, export, resolve the review -- end
to end through the stdio server. That gap is why `cassette_review_completion`'s required `reason`
argument could go unmentioned in the skill for so long: the transport tests always passed it
because they were written from the signature, and the protocol tests never reached an export.

The API here is the same contract implementation the transport tests use, served over real HTTP
on a real port. The MCP server is the real child process a host launches.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import AnyUrl

from mcp_plugin.server import RetainedFileResource
from test_api_transport_mock import EXPORT_BYTES, _MockCassetteAPI


ROOT = Path(__file__).resolve().parents[1]


def _serve_cassette_api() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockCassetteAPI)
    server.rec = {  # type: ignore[attr-defined]
        "requests": [],
        "put_count": 0,
        "init_count": 0,
        "complete_count": 0,
        "init_bodies": [],
        "complete_bodies": [],
        "upload_session_ids": [],
        "upload_project_ids": [],
        "auth_email": None,
        "resume_value": None,
        "run_input": None,
        "run_config": None,
        "thread_metadata": None,
        "export_session": None,
        "media_ready_polls": 0,
    }
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _environment(tmp_path: Path, media_root: Path, port: int) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("CASSETTE_")}
    environment.update(
        {
            "CASSETTE_RUNTIME_ADAPTER": "mcp",
            "CASSETTE_CONFIG_HOME": str(tmp_path / "config"),
            "CASSETTE_DATA_HOME": str(tmp_path / "data"),
            "CASSETTE_ASSET_ROOT": str(tmp_path / "assets"),
            "CASSETTE_ALLOWED_SOURCE_ROOTS": str(media_root),
            "CASSETTE_ALLOWED_EXTENSIONS": ".mp4,.jpg,.png,.mp3",
            "CASSETTE_MAX_BYTES": "1000000",
            "CASSETTE_MIN_JOB_TIMEOUT_SEC": "0",
            "CASSETTE_API_URL": f"http://127.0.0.1:{port}",
            "CASSETTE_AUTH_EMAIL": "e@x.io",
            "CASSETTE_AUTH_PASSWORD": "pw",
            "CASSETTE_API_POLL_INTERVAL_SEC": "1",
            # Deliberately no CASSETTE_API_AUTO_EXPORT: rendering must require BOTH an explicit
            # export turn and an explicit review decision, which is the contract being asserted.
        }
    )
    return environment


def test_mcp_flow_ingest_converse_export_reaches_a_validated_artifact(tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    clip = media_root / "demo.mp4"
    clip.write_bytes(b"x" * 4096)

    server = _serve_cassette_api()
    port = server.server_address[1]
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_plugin.server"],
        cwd=str(ROOT),
        env=_environment(tmp_path, media_root, port),
    )

    async def exercise() -> None:
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                ingested = (
                    await session.call_tool("cassette_ingest_media", {"source_path": str(clip)})
                ).structuredContent
                assert ingested["ok"] is True, ingested
                session_id = ingested["session_id"]

                listed = (await session.call_tool("cassette_list_assets", {"session_id": session_id})).structuredContent
                assert len(listed["data"]["manifest"]["assets"]) == 1
                configured = (await session.call_tool("cassette_config", {"session_id": session_id})).structuredContent
                assert configured["ok"] is True

                # A conversational turn commits the edit and renders NOTHING.
                turn = (
                    await session.call_tool(
                        "cassette_run_job",
                        {"session_id": session_id, "message": "trim the dead air off the front"},
                    )
                ).structuredContent
                assert turn["phase"] == "succeeded", turn
                assert not turn["artifacts"], "a non-export turn must not produce a rendered artifact"
                # The per-turn preview hangs off data.job, not the top of the envelope. Reading it
                # from the envelope root returns None, which is indistinguishable from "no preview".
                assert turn["data"]["job"]["quality"]["timeline_ctl"].startswith(f"TIMELINE {session_id} v7")
                receipt = turn["data"]["job"]["quality"]["analysis_receipts"][0]
                assert receipt["model"] == "gemini-3.8-flash"
                assert receipt["processing"] == "agentic"
                assert receipt["store"] is False
                assert "googleFileUri" not in receipt
                assert not any(path.startswith("/api/export/projects/") for _, path in server.rec["requests"])

                # cassette_timeline answers in its OWN shape: data.ctl, not data.job.quality.
                timeline = (
                    await session.call_tool("cassette_timeline", {"session_id": session_id, "contact_sheet": True})
                ).structuredContent
                assert timeline["ok"] is True
                assert timeline["data"]["ctl"].startswith(f"TIMELINE {session_id} v7")
                assert timeline["data"]["clips"][0]["duration_sec"] == 3.0

                # An export turn stops for review rather than rendering on its own.
                export_turn = (
                    await session.call_tool(
                        "cassette_run_job",
                        {"session_id": session_id, "message": "export it", "export": True},
                    )
                ).structuredContent
                assert export_turn["phase"] == "review_required", export_turn
                job_id = export_turn["job_id"]

                # `reason` is required. Omitting it fails BEFORE anything renders -- the exact
                # trap a skill that documents only job_id and decision walks its reader into.
                without_reason = (
                    await session.call_tool("cassette_review_completion", {"job_id": job_id, "decision": "export"})
                ).structuredContent
                assert without_reason["ok"] is False
                assert without_reason["error"]["code"] == "validation_error"
                assert server.rec["export_session"] is None, "a rejected review must not render"

                resolved = (
                    await session.call_tool(
                        "cassette_review_completion",
                        {
                            "job_id": job_id,
                            "decision": "export",
                            "reason": "user asked to export and the timeline is non-empty",
                        },
                    )
                ).structuredContent
                assert resolved["phase"] == "exported", resolved

                artifacts = resolved["artifacts"]
                assert artifacts, "an exported turn must carry the deliverable"
                delivered = Path(artifacts[0]["path"])
                assert delivered.is_file()
                assert delivered.read_bytes() == EXPORT_BYTES
                # Containment: exports are only ever handed back from under the data root.
                assert "exports" in delivered.parts

    try:
        asyncio.run(exercise())
    finally:
        server.shutdown()
        server.server_close()

    rec = server.rec
    assert rec["auth_email"] == "e@x.io"
    # Uploaded exactly once across both turns -- a re-upload per turn would be a silent cost bug.
    assert (rec["init_count"], rec["put_count"], rec["complete_count"]) == (1, 1, 1)
    assert rec["media_ready_polls"] >= 1, "the run must wait for media readiness before editing"
    assert rec["export_session"] is not None, "the backend never received the export"


@pytest.mark.parametrize("decision", ["continue", "needs_user", "failed"])
def test_only_an_export_decision_renders(tmp_path, decision):
    """The review gate is the last thing standing between a judgement and a render."""
    media_root = tmp_path / "media"
    media_root.mkdir()
    clip = media_root / "demo.mp4"
    clip.write_bytes(b"x" * 4096)

    server = _serve_cassette_api()
    port = server.server_address[1]
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_plugin.server"],
        cwd=str(ROOT),
        env=_environment(tmp_path, media_root, port),
    )

    async def exercise() -> None:
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                ingested = (
                    await session.call_tool("cassette_ingest_media", {"source_path": str(clip)})
                ).structuredContent
                session_id = ingested["session_id"]
                export_turn = (
                    await session.call_tool(
                        "cassette_run_job",
                        {"session_id": session_id, "message": "export it", "export": True},
                    )
                ).structuredContent
                assert export_turn["phase"] == "review_required"
                resolved = (
                    await session.call_tool(
                        "cassette_review_completion",
                        {
                            "job_id": export_turn["job_id"],
                            "decision": decision,
                            "reason": f"probe: {decision}",
                        },
                    )
                ).structuredContent
                assert resolved["phase"] != "exported", resolved

    try:
        asyncio.run(exercise())
    finally:
        server.shutdown()
        server.server_close()

    assert server.rec["export_session"] is None, f"decision={decision} must not render"


def test_artifact_resource_checks_deadline_on_every_read(tmp_path, monkeypatch):
    artifact = tmp_path / "artifact.mp4"
    artifact.write_bytes(b"video")
    clock = tmp_path / "clock.txt"
    clock.write_text("1000", encoding="utf-8")
    monkeypatch.setenv("CASSETTE_RETENTION_TEST_MODE", "1")
    monkeypatch.setenv("CASSETTE_RETENTION_TEST_CLOCK_FILE", str(clock))
    resource = RetainedFileResource(
        uri=AnyUrl(artifact.as_uri()),
        name="artifact.mp4",
        path=artifact,
        mime_type="video/mp4",
        is_binary=True,
        expires_at="1970-01-01T00:16:41Z",
    )

    assert asyncio.run(resource.read()) == b"video"
    clock.write_text("1001", encoding="utf-8")
    with pytest.raises(ValueError, match="24-hour retention deadline"):
        asyncio.run(resource.read())
