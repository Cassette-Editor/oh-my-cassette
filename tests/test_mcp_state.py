from __future__ import annotations


import pytest

from mcp_plugin.models import SessionPhase
from mcp_plugin.state import InvalidTransition, StateStore, phase_from_job


def test_typed_state_machine_rejects_invalid_export_transition(tmp_path):
    store = StateStore(tmp_path / "state")
    state = store.transition("session", SessionPhase.READY)
    assert state.phase == SessionPhase.READY
    with pytest.raises(InvalidTransition) as invalid:
        store.transition("session", SessionPhase.EXPORTED)
    assert invalid.value.current == SessionPhase.READY
    assert invalid.value.target == SessionPhase.EXPORTED


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        ({"status": "running"}, SessionPhase.RUNNING),
        ({"status": "needs_user", "quality": {}}, SessionPhase.NEEDS_USER),
        (
            {"status": "needs_user", "quality": {"completion_review_required": True}},
            SessionPhase.REVIEW_REQUIRED,
        ),
        ({"status": "succeeded", "outputs": [{"local_path": "/tmp/x"}]}, SessionPhase.EXPORTED),
        ({"status": "failed"}, SessionPhase.FAILED),
        ({"status": "cancelled"}, SessionPhase.CANCELLED),
    ],
)
def test_job_phase_is_derived_from_typed_persisted_fields(job, expected):
    assert phase_from_job(job) == expected


def test_next_action_appends_editor_url_on_live_phases():
    from mcp_plugin.state import next_action_for
    from mcp_plugin.models import SessionPhase

    url = "http://127.0.0.1:8080/try?projectSessionId=abc&chatSessionId=u-1"
    running = next_action_for(SessionPhase.RUNNING, job_id="j1", editor_url=url)
    assert url in running and running.startswith("Call cassette_job_status")
    needs_user = next_action_for(SessionPhase.NEEDS_USER, job_id="j1", editor_url=url)
    assert url in needs_user
    # Mechanical phases never carry the link.
    assert url not in next_action_for(SessionPhase.NEW, editor_url=url)
    assert url not in next_action_for(SessionPhase.SUCCEEDED, editor_url=url)
