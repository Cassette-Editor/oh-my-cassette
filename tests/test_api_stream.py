"""SSE event-channel tests: frame parsing, delta folding onto the job, and the off switch.

The stream is an enhancement channel — run completion stays poll-driven — so these tests
cover the parser, the fold-onto-job behavior, and that CASSETTE_API_STREAM=0 disables it.
"""

from __future__ import annotations

import io
import json
import threading
import time

from cassette import jobs
from cassette.api_transport import ApiTransport, _stream_enabled


def _sse(*frames: tuple[str, dict | str]) -> io.BytesIO:
    chunks = []
    for event, data in frames:
        payload = data if isinstance(data, str) else json.dumps(data)
        chunks.append(f"event: {event}\n")
        for line in payload.split("\n"):
            chunks.append(f"data: {line}\n")
        chunks.append("\n")
    return io.BytesIO("".join(chunks).encode("utf-8"))


def test_iter_sse_parses_events_and_multiline_data():
    frames = list(
        ApiTransport._iter_sse(
            _sse(
                ("custom", {"type": "project_operation_committed"}),
                ("metadata", {"run_id": "r-1"}),
                ("custom", "line1\nline2"),
            )
        )
    )
    assert frames[0] == ("custom", '{"type": "project_operation_committed"}')
    assert frames[1][0] == "metadata"
    assert frames[2] == ("custom", "line1\nline2")


def test_iter_sse_ignores_comments_and_handles_missing_trailing_blank():
    body = io.BytesIO(b': keepalive\nevent: custom\ndata: {"a": 1}')
    frames = list(ApiTransport._iter_sse(body))
    assert frames == [("custom", '{"a": 1}')]


def test_stream_enabled_flag(monkeypatch):
    monkeypatch.delenv("CASSETTE_API_STREAM", raising=False)
    assert _stream_enabled() is True
    monkeypatch.setenv("CASSETTE_API_STREAM", "0")
    assert _stream_enabled() is False


def test_consume_run_stream_folds_delta_and_progress(cassette_env, monkeypatch):
    job = jobs.create_job("sse", "prompt", None, [], {})
    doc_v2 = {
        "schemaVersion": 2,
        "projectId": "try-session-sse",
        "version": 2,
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
                    "durationInFrames": 60,
                }
            },
            "transitions": {},
        },
        "order": {"trackIds": ["t1"], "clipIds": ["c1"], "transitionIds": []},
    }

    transport = ApiTransport()
    # No project yet -> baseline falls back to the empty document.
    monkeypatch.setattr(
        ApiTransport, "get_project_document", lambda self, sid: (_ for _ in ()).throw(RuntimeError("404"))
    )
    stream = _sse(
        ("custom", {"type": "plan_progress", "label": "understanding request"}),
        ("custom", {"type": "project_operation_committed", "document": doc_v2}),
    )

    class _Resp:
        def __enter__(self):
            return stream

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("cassette.api_transport.urlopen", lambda *a, **k: _Resp())
    transport._consume_run_stream("th-1", "r-1", job, threading.Event())

    saved = jobs.load_job(job["job_id"])
    assert saved["plan_progress"] == ["understanding request"]
    assert saved["timeline_delta"].startswith("CHANGES v0 -> v2")
    assert "+ V1/A intro.mp4" in saved["timeline_delta"]


def test_consume_run_stream_survives_garbage(cassette_env, monkeypatch):
    job = jobs.create_job("sse2", "prompt", None, [], {})
    transport = ApiTransport()
    monkeypatch.setattr(ApiTransport, "get_project_document", lambda self, sid: {"version": 0, "entities": {}})
    stream = _sse(("custom", "not-json"), ("weird", {"x": 1}))

    class _Resp:
        def __enter__(self):
            return stream

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("cassette.api_transport.urlopen", lambda *a, **k: _Resp())
    transport._consume_run_stream("th-1", "r-1", job, threading.Event())
    saved = jobs.load_job(job["job_id"])
    assert saved["timeline_delta"] is None


def test_refresh_stream_listener_restarts_on_new_run(cassette_env, monkeypatch):
    transport = ApiTransport()
    started: list[str] = []

    def fake_consume(self, thread_id, run_id, job, stop):
        started.append(run_id)
        while not stop.is_set():
            time.sleep(0.01)

    monkeypatch.setattr(ApiTransport, "_consume_run_stream", fake_consume)
    job = {"job_id": "job-x"}
    stop1 = transport._refresh_stream_listener("th", "r-1", job, None)
    assert stop1 is not None and getattr(stop1, "run_id", None) == "r-1"
    same = transport._refresh_stream_listener("th", "r-1", job, stop1)
    assert same is stop1
    stop2 = transport._refresh_stream_listener("th", "r-2", job, stop1)
    assert stop1.is_set() and not stop2.is_set()
    stop2.set()
    time.sleep(0.05)
    assert started == ["r-1", "r-2"]


def test_stream_disabled_starts_nothing(cassette_env, monkeypatch):
    monkeypatch.setenv("CASSETTE_API_STREAM", "0")
    transport = ApiTransport()
    assert transport._refresh_stream_listener("th", "r-1", {"job_id": "j"}, None) is None
