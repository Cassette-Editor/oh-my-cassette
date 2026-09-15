"""The pydantic mirrors accept real backend payloads captured in contracts/."""

from __future__ import annotations

import json
from pathlib import Path

from oh_my_cassette.cassette.models import (
    DURABLE_EVENT_TYPES,
    ChatSession,
    ImportManifest,
    MediaFile,
    MediaOperationStatus,
    ProjectSnapshot,
    RunEvent,
    SessionState,
    StartRunResult,
    TimelineHistory,
    TransientEvent,
)

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"


def load(name: str):
    return json.loads((CONTRACTS / name).read_text("utf-8"))


def test_project_snapshot_contract():
    snapshot = ProjectSnapshot.model_validate(load("project-snapshot.json"))
    assert snapshot.kind == "project_document_snapshot"
    assert snapshot.version == 0
    assert snapshot.document["activeSequenceId"] == "seq_main"
    assert snapshot.meta is not None and snapshot.meta.historyCursorSequence == 0


def test_chat_session_contract():
    session = ChatSession.model_validate(load("chat-session.json")["session"])
    assert session.mode == "chat" and session.status == "active"


def test_session_state_contract():
    state = SessionState.model_validate(load("session-state.json"))
    assert state.activeRun is None
    assert state.controlRevision == 0 and state.eventCursor == 0
    assert state.executionPreference is not None
    assert state.executionPreference.preferredModelId == "openai/gpt-5.6-luna"


def test_start_result_contract():
    body = load("command-start-result.json")
    assert body["state"] == "applied"
    result = StartRunResult.model_validate(body["result"])
    assert result.status == "running" and result.eventCursor == 1


def test_run_events_contract():
    durable, transient = [], []
    for line in (CONTRACTS / "run-events.jsonl").read_text("utf-8").splitlines():
        data = json.loads(line)
        if data.get("replayable") is False:
            transient.append(TransientEvent.model_validate(data))
        else:
            durable.append(RunEvent.model_validate(data))
    assert [e.type for e in durable] == [
        "run_started",
        "assistant_message_started",
        "assistant_message_committed",
        "run_terminal",
    ]
    assert all(e.type in DURABLE_EVENT_TYPES for e in durable)
    assert [e.sequence for e in durable] == [1, 2, 3, 4]
    assert transient[0].type == "assistant_text_delta" and transient[0].payload == {"text": "pong"}
    terminal = durable[-1].payload or {}
    assert terminal["status"] == "completed" and terminal["finalText"] == "pong"


def test_media_status_contract():
    statuses = [
        MediaOperationStatus.model_validate(s) for s in load("media-operations-status.json")["statuses"]
    ]
    assert {s.fileName for s in statuses} == {"tone.mp3", "slide.png"}
    assert all(s.playbackReady and not s.fullyReady and not s.failed for s in statuses)


def test_media_files_contract():
    files = [MediaFile.model_validate(f) for f in load("media-files.json")["files"]]
    assert {f.file_type for f in files} == {"audio", "image"}
    assert all(f.upload_status == "completed" for f in files)


def test_import_manifest_contract():
    manifest = ImportManifest.model_validate(load("import-manifest.json"))
    assert manifest.files and manifest.files[0].kind in {"audio", "image", "video"}
    assert manifest.request["sessionId"].startswith("try-session-")


def test_timeline_history_contract():
    history = TimelineHistory.model_validate(load("timeline-history.json"))
    assert history.cursorSequence >= 1
    group = history.groups[0]
    assert group.source == "agent" and group.versionAfter == group.versionBefore + 1
