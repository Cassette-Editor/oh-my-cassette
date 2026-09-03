from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_local_mcp_harness_generates_canonical_18_second_fixture(tmp_path):
    from scripts import e2e_local_mcp

    fixture = e2e_local_mcp.generate_fixture(tmp_path)
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-show_entries",
            "stream=codec_type,width,height,r_frame_rate",
            "-of",
            "json",
            str(fixture),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(proc.stdout)
    video = next(stream for stream in payload["streams"] if stream["codec_type"] == "video")
    assert float(payload["format"]["duration"]) == 18.0
    assert (video["width"], video["height"], video["r_frame_rate"]) == (1280, 720, "30/1")
    assert any(stream["codec_type"] == "audio" for stream in payload["streams"])


def test_agentic_receipt_accepts_google_stateless_null_response_id():
    from scripts import e2e_local_mcp

    receipt = {
        "provider": "google",
        "model": "gemini-3.8-flash",
        "api": "interactions",
        "processing": "agentic",
        "fileTransport": "google_files",
        "serviceTier": "standard",
        "store": False,
        "responseId": None,
        "agenticNavigationStepCount": 2,
        "startedAt": "2026-09-02T00:00:00Z",
        "completedAt": "2026-09-02T00:00:03Z",
        "evidenceCount": 4,
        "expiresAt": "2026-09-03T00:00:00Z",
    }

    assert (
        e2e_local_mcp._assert_analysis_receipt(
            {"data": {"job": {"quality": {"analysis_receipts": [receipt]}}}},
            expires_at="2026-09-03T00:00:00Z",
        )
        == receipt
    )


def test_agentic_receipt_allows_the_server_deadline_after_the_earlier_plugin_deadline():
    from scripts import e2e_local_mcp

    receipt = {
        "provider": "google",
        "model": "gemini-3.8-flash",
        "api": "interactions",
        "processing": "agentic",
        "fileTransport": "google_files",
        "serviceTier": "standard",
        "store": False,
        "responseId": None,
        "agenticNavigationStepCount": 2,
        "startedAt": "2026-09-02T00:00:00Z",
        "completedAt": "2026-09-02T00:00:03Z",
        "evidenceCount": 4,
        "expiresAt": "2026-09-03T00:00:02Z",
    }

    assert (
        e2e_local_mcp._assert_analysis_receipt(
            {"data": {"job": {"quality": {"analysis_receipts": [receipt]}}}},
            expires_at="2026-09-03T00:00:00Z",
        )
        == receipt
    )


def test_agentic_receipt_rejects_a_server_deadline_before_the_plugin_deadline():
    from scripts import e2e_local_mcp

    receipt = {
        "provider": "google",
        "model": "gemini-3.8-flash",
        "api": "interactions",
        "processing": "agentic",
        "fileTransport": "google_files",
        "serviceTier": "standard",
        "store": False,
        "responseId": None,
        "agenticNavigationStepCount": 2,
        "startedAt": "2026-09-02T00:00:00Z",
        "completedAt": "2026-09-02T00:00:03Z",
        "evidenceCount": 4,
        "expiresAt": "2026-09-02T23:59:59Z",
    }

    with pytest.raises(e2e_local_mcp.AcceptanceError, match="precedes plugin deadline"):
        e2e_local_mcp._assert_analysis_receipt(
            {"data": {"job": {"quality": {"analysis_receipts": [receipt]}}}},
            expires_at="2026-09-03T00:00:00Z",
        )


def test_agentic_receipt_rejects_legacy_field_names():
    from scripts import e2e_local_mcp

    receipt = {
        "provider": "google",
        "model": "gemini-3.8-flash",
        "apiType": "interactions",
        "processing": "agentic",
        "transport": "files_api",
        "serviceTier": "standard",
        "store": False,
        "responseId": None,
        "agenticNavigationSteps": 2,
    }
    with pytest.raises(e2e_local_mcp.AcceptanceError, match="canonical non-sensitive fields"):
        e2e_local_mcp._assert_analysis_receipt(
            {"data": {"job": {"quality": {"analysis_receipts": [receipt]}}}},
            expires_at="2026-09-03T00:00:00Z",
        )


def test_ingest_deadline_comes_from_mcp_envelope_data():
    from scripts import e2e_local_mcp

    envelope = {
        "ok": True,
        "expires_at": "wrong-level",
        "data": {"expires_at": "2026-09-03T12:00:00Z"},
    }

    assert e2e_local_mcp._ingest_expires_at(envelope) == "2026-09-03T12:00:00Z"


def test_independent_qc_decodes_blue_moving_circle_cut(tmp_path):
    from scripts import e2e_local_mcp

    fixture = e2e_local_mcp.generate_fixture(tmp_path / "fixture")
    export = tmp_path / "export.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "6",
            "-i",
            str(fixture),
            "-t",
            "6",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "30",
            "-c:a",
            "aac",
            str(export),
        ],
        check=True,
        timeout=180,
    )

    measured = e2e_local_mcp._assert_independent_export_qc(export)

    assert 5.9 <= measured["duration_sec"] <= 6.1
    assert measured["audio_span_sec"] >= 5.5
    assert not measured["black_segments_sec"]
    assert (
        max(item["circle_centroid_x"] for item in measured["frame_metrics"])
        - min(item["circle_centroid_x"] for item in measured["frame_metrics"])
        > 40
    )


def test_acceptance_hook_contract_rejects_false_remote_cleanup():
    from scripts import e2e_local_mcp

    with pytest.raises(e2e_local_mcp.AcceptanceError, match="accessibleGoogleFileCount"):
        e2e_local_mcp._assert_remote_retention(
            {
                "sessionId": "s",
                "sweepCompleted": True,
                "accessibleServerObjectCount": 0,
                "accessibleGoogleFileCount": 1,
                "queueReferenceCount": 0,
                "idempotent": False,
            },
            session_id="s",
            idempotent=False,
        )


@pytest.mark.e2e
def test_weixin_e2e_harness_reports_latest_job():
    required = ["CASSETTE_E2E_JOB_ROOT", "CASSETTE_MEDIA_DIR"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        pytest.skip(f"missing E2E environment variables: {', '.join(missing)}")

    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "e2e_weixin_cassette.py")],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=int(os.getenv("CASSETTE_E2E_TIMEOUT_SEC", "1800")) + 30,
        check=False,
    )
    assert proc.stdout.strip(), proc.stderr
    payload = json.loads(proc.stdout)
    assert {"success", "job_id", "status", "manifest_path", "result_path", "output_links", "errors"} <= payload.keys()
    assert "prompt" not in proc.stdout
    assert "asset_paths" not in proc.stdout
    assert "worker_command" not in proc.stdout
    if not payload["success"]:
        pytest.fail(f"E2E harness did not report success: {payload}")


@pytest.mark.e2e
def test_local_cassette_e2e_harness_runs():
    if not os.getenv("CASSETTE_URL"):
        pytest.skip("missing CASSETTE_URL")
    media = ROOT / "tests" / "fixtures" / "sample.mp4"
    if not media.exists():
        pytest.skip("missing tests/fixtures/sample.mp4")

    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "e2e_local_cassette.py"),
            "--media",
            str(media),
            "--instruction",
            "帮我剪成 10 秒以内的短视频，加中文字幕",
        ],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=int(os.getenv("CASSETTE_E2E_TIMEOUT_SEC", "1800")) + 30,
        check=False,
    )
    assert proc.stdout.strip(), proc.stderr
    payload = json.loads(proc.stdout)
    assert {"success", "job_id", "status", "manifest_path", "result_path", "output_links", "errors"} <= payload.keys()
    assert "prompt" not in proc.stdout
    assert "asset_paths" not in proc.stdout
    assert "worker_command" not in proc.stdout
    if not payload["success"]:
        pytest.fail(f"Local Cassette E2E harness did not report success: {payload}")
