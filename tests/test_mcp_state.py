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
        (
            {"status": "succeeded", "quality": {"completion_observed": False}},
            SessionPhase.FAILED,
        ),
        ({"status": "failed"}, SessionPhase.FAILED),
        ({"status": "cancelled"}, SessionPhase.CANCELLED),
    ],
)
def test_job_phase_is_derived_from_typed_persisted_fields(job, expected):
    assert phase_from_job(job) == expected


def test_next_action_never_carries_an_editor_link():
    """The editor deep link is a bearer capability — next_action must never surface one."""
    from mcp_plugin.state import next_action_for
    from mcp_plugin.models import SessionPhase

    for phase in SessionPhase:
        action = next_action_for(phase, job_id="j1")
        assert "projectSessionId" not in action
        assert "Watch live" not in action
        assert "http://" not in action and "https://" not in action
    assert next_action_for(SessionPhase.RUNNING, job_id="j1").startswith("Call cassette_job_status")


def test_review_required_next_action_does_not_request_redundant_export_confirmation():
    from mcp_plugin.models import SessionPhase
    from mcp_plugin.state import next_action_for

    action = next_action_for(SessionPhase.REVIEW_REQUIRED)
    assert "same assistant turn" in action
    assert "do not ask the user to confirm export again" in action


def test_multi_turn_transitions_and_next_actions(tmp_path):
    from mcp_plugin.state import _ALLOWED, next_action_for
    from mcp_plugin.models import SessionPhase

    # A settled turn can flow straight into the next run.
    assert SessionPhase.RUNNING in _ALLOWED[SessionPhase.GUIDED_CHOICES]
    assert SessionPhase.RUNNING in _ALLOWED[SessionPhase.SUCCEEDED]
    assert SessionPhase.RUNNING in _ALLOWED[SessionPhase.EXPORTED]

    succeeded = next_action_for(SessionPhase.SUCCEEDED)
    assert "nothing was rendered" in succeeded
    assert "export=true" in succeeded
    assert "cassette_run_job(message=...)" in succeeded

    exported = next_action_for(SessionPhase.EXPORTED)
    assert "artifact" in exported

    ready = next_action_for(SessionPhase.READY)
    assert "verbatim" in ready
