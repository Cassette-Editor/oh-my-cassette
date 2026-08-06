from __future__ import annotations

import pytest

from cassette.core import jobs, notifier, tools, transport
from cassette.core.api_transport import ApiTransport
from cassette.core.transport import Transport, get_transport, selected_transport


@pytest.fixture(autouse=True)
def _isolate_hermes_env(monkeypatch, tmp_path):
    # Transport env resolution falls back to ~/.hermes/.env; point it at an absent file so these
    # hermetic tests never read the developer's real Hermes credentials.
    monkeypatch.setenv("HERMES_ENV_FILE", str(tmp_path / "absent.env"))


@pytest.fixture(autouse=True)
def _reset_retirement_notice(monkeypatch):
    # The notice is once-per-process by design, which would make its test order-dependent.
    monkeypatch.setattr(transport, "_RETIRED_NOTICE_SHOWN", False)


def test_the_only_transport_is_the_api_transport(monkeypatch):
    monkeypatch.delenv("CASSETTE_TRANSPORT", raising=False)
    assert selected_transport() == "api"
    assert isinstance(get_transport(), ApiTransport)
    assert isinstance(ApiTransport(), Transport)


@pytest.mark.parametrize("value", ["browser", "BROWSER", " browser ", "api", "", "weird"])
def test_no_env_value_can_select_anything_but_the_api_transport(monkeypatch, value):
    # CASSETTE_TRANSPORT was the selector. A stale value must not resurrect a second code path,
    # and must not fail the call either — the API path is what the setting's users wanted anyway.
    monkeypatch.setenv("CASSETTE_TRANSPORT", value)
    assert isinstance(get_transport(), ApiTransport)


def test_a_retired_browser_setting_is_reported_on_stderr_and_only_once(monkeypatch, capsys):
    monkeypatch.setenv("CASSETTE_TRANSPORT", "browser")
    get_transport()
    first = capsys.readouterr()
    # stdout carries MCP protocol frames; a diagnostic there corrupts the session.
    assert first.out == ""
    assert "CASSETTE_TRANSPORT=browser is retired" in first.err

    get_transport()
    assert capsys.readouterr().err == ""


def test_no_notice_when_the_setting_is_absent_or_already_api(monkeypatch, capsys):
    monkeypatch.delenv("CASSETTE_TRANSPORT", raising=False)
    get_transport()
    assert capsys.readouterr().err == ""
    monkeypatch.setenv("CASSETTE_TRANSPORT", "api")
    get_transport()
    assert capsys.readouterr().err == ""


def test_transport_readiness_gate_reports_api_readiness(monkeypatch):
    # tools.check_transport_ready is the readiness gate; the API origin defaults to the deployed
    # Cassette, so readiness is credential-gated.
    for var in (
        "CASSETTE_AUTH_EMAIL",
        "CASSETTE_AUTH_ACCOUNT",
        "CASSETTE_EMAIL",
        "CASSETTE_AUTH_PASSWORD",
        "CASSETTE_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)
    assert tools.check_transport_ready() is False
    monkeypatch.setenv("CASSETTE_AUTH_EMAIL", "e@x.io")
    monkeypatch.setenv("CASSETTE_AUTH_PASSWORD", "pw")
    assert tools.check_transport_ready() is True


def test_api_transport_availability_gating(monkeypatch):
    # The API origin defaults to the deployed Cassette, so availability is gated on credentials.
    for var in (
        "CASSETTE_AUTH_EMAIL",
        "CASSETTE_AUTH_ACCOUNT",
        "CASSETTE_EMAIL",
        "CASSETTE_AUTH_PASSWORD",
        "CASSETTE_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)
    assert ApiTransport().check_available() is False
    monkeypatch.setenv("CASSETTE_AUTH_EMAIL", "e@x.io")
    monkeypatch.setenv("CASSETTE_AUTH_PASSWORD", "pw")
    assert ApiTransport().check_available() is True
    monkeypatch.delenv("CASSETTE_AUTH_PASSWORD", raising=False)
    assert ApiTransport().check_available() is False


def test_api_transport_run_fails_clean_without_credentials(cassette_env, monkeypatch):
    for var in (
        "CASSETTE_AUTH_EMAIL",
        "CASSETTE_AUTH_ACCOUNT",
        "CASSETTE_EMAIL",
        "CASSETTE_AUTH_PASSWORD",
        "CASSETTE_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)
    result = get_transport().run_job(
        {"job_id": "job-x", "session_hash": "s", "cassette_session_id": "s", "prompt": "edit", "asset_paths": []}
    )
    # Misconfiguration is a structured terminal failure, not a crash. No network is touched
    # because credentials are validated before any request.
    assert result["status"] == "failed"
    assert set(result) >= {"status", "outputs", "questions", "errors", "quality", "final_screenshot"}
    assert result["errors"] and result["errors"][0]["code"] == "auth_missing_credentials"


def test_connectivity_probe_targets_the_api_origin_health_endpoint(monkeypatch):
    from cassette.core import api_transport as api_mod

    seen: dict = {}

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def _fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        return _Response()

    monkeypatch.setattr(api_mod, "urlopen", _fake_urlopen)
    result = api_mod.check_cassette_connectivity("https://cassette.example")
    assert result == {"ok": True, "status": "reachable", "http_status": 200}
    # /healthz is public, so an unprivileged or expired credential still reports reachable.
    assert seen["url"] == "https://cassette.example/healthz"
    assert seen["method"] == "GET"


def _make_job():
    return jobs.create_job(
        session_hash="sess",
        prompt="edit it",
        instruction=None,
        asset_paths=[],
        options={"cassette_session_id": "sess"},
    )


def _canonical_succeeded(local_path: str) -> dict:
    """The job-result shape jobs/notifier/_scrub_job/_job_report consume."""
    return {
        "status": "succeeded",
        "outputs": [
            {
                "text": "out.mp4",
                "href": "/api/export/jobs/x/file",
                "download": "out.mp4",
                "local_path": local_path,
                "kind": "video",
            }
        ],
        "questions": [],
        "errors": [],
        "quality": {
            "completion_observed": True,
            "export_completed": True,
            "export_pending": False,
            "output_link_count": 1,
            "local_output_count": 1,
            "risk": "low",
        },
        "final_screenshot": None,
    }


def test_transport_results_match_the_contract_downstream_expects(cassette_env, tmp_path):
    mp4 = tmp_path / "out.mp4"
    mp4.write_bytes(b"video")
    expected = _canonical_succeeded(str(mp4))
    produced = ApiTransport()._result(
        "succeeded",
        outputs=[
            {
                "text": "out.mp4",
                "href": "/api/export/jobs/y/file",
                "download": "out.mp4",
                "local_path": str(mp4),
                "kind": "video",
            }
        ],
        completion_observed=True,
        export_completed=True,
        risk="low",
    )
    assert set(expected) == set(produced)
    assert set(expected["quality"]) <= set(produced["quality"])


def test_report_and_output_scrubbing_on_a_succeeded_result(cassette_env, tmp_path):
    mp4 = tmp_path / "out.mp4"
    mp4.write_bytes(b"video")

    job = _make_job()
    job.update(
        ApiTransport()._result(
            "succeeded",
            outputs=[
                {
                    "text": "out.mp4",
                    "href": "/api/export/jobs/y/file",
                    "download": "out.mp4",
                    "local_path": str(mp4),
                    "kind": "video",
                }
            ],
            completion_observed=True,
            export_completed=True,
            risk="low",
        )
    )
    job["status"] = "succeeded"

    scrubbed = tools._scrub_job(job)
    assert scrubbed["report"]["status"] == "succeeded"
    assert scrubbed["report"]["output_count"] == 1
    assert scrubbed["report"]["export_pending"] is False
    # local_path is stripped; downloaded+filename are added.
    out = scrubbed["outputs"][0]
    assert "local_path" not in out
    assert out["downloaded"] is True
    assert out["filename"] == "out.mp4"


def test_notifier_delivers_only_exports_that_exist_on_disk(cassette_env, tmp_path):
    real = tmp_path / "v.mp4"
    real.write_bytes(b"video")
    missing = tmp_path / "missing.mp4"

    assert notifier._exported_media_paths({"outputs": [{"local_path": str(real), "kind": "video"}]}) == [str(real)]
    assert notifier._exported_media_paths({"outputs": [{"local_path": str(missing), "kind": "video"}]}) == []

    api_real = ApiTransport()._result(
        "succeeded",
        outputs=[{"text": "v", "href": "h", "download": "v", "local_path": str(real), "kind": "video"}],
    )
    assert notifier._exported_media_paths(api_real) == [str(real)]


def _fake_result(via: str) -> dict:
    return {
        "status": "succeeded",
        "_via": via,
        "outputs": [],
        "questions": [],
        "errors": [],
        "quality": {},
        "final_screenshot": None,
    }


@pytest.mark.parametrize(
    "label,expected",
    [
        ("GPT-5.6 Luna", "openai/gpt-5.6-luna"),
        ("gpt 5.6 luna", "openai/gpt-5.6-luna"),
        ("GPT-5.4 Mini", "openai/gpt-5.4-mini"),
        ("openai/gpt-5.4-mini", "openai/gpt-5.4-mini"),  # already an id
        ("", "openai/gpt-5.6-luna"),  # no choice -> default
    ],
)
def test_api_model_label_maps_to_id(label, expected):
    # The user's UI model *label* is honored (mapped to an agent model id), not dropped for the default.
    assert ApiTransport._resolve_model_id({"model_selection": {"model": label}}) == expected


def test_api_model_selection_required_fails_on_unmappable_label(monkeypatch):
    from cassette.core.api_transport import ApiTransportError

    monkeypatch.delenv("CASSETTE_REQUIRE_MODEL_SELECTION", raising=False)  # default true
    monkeypatch.delenv("CASSETTE_API_MODEL_ID", raising=False)
    with pytest.raises(ApiTransportError) as exc:
        ApiTransport._resolve_model_id({"model_selection": {"model": "Totally Unknown Model"}})
    assert exc.value.code == "model_selection_failed"
    monkeypatch.setenv("CASSETTE_REQUIRE_MODEL_SELECTION", "off")
    assert (
        ApiTransport._resolve_model_id({"model_selection": {"model": "Totally Unknown Model"}}) == "openai/gpt-5.6-luna"
    )


def test_removed_deepseek_model_is_rejected(monkeypatch):
    from cassette.core.api_transport import ApiTransportError

    monkeypatch.delenv("CASSETTE_API_MODEL_ID", raising=False)
    with pytest.raises(ApiTransportError) as exc:
        ApiTransport._resolve_model_id({"model_selection": {"model": "DeepSeek V4 Flash"}})
    assert exc.value.code == "model_selection_failed"


@pytest.mark.parametrize("thinking", ["off", "minimal", "low", "medium", "high", "xhigh"])
def test_api_thinking_config_uses_gpt_reasoning_levels(monkeypatch, thinking):
    monkeypatch.delenv("CASSETTE_API_THINKING", raising=False)
    assert ApiTransport._resolve_thinking_config({"model_selection": {"thinking_level": thinking.upper()}}) == thinking


def test_api_thinking_config_rejects_non_gpt_level(monkeypatch):
    monkeypatch.delenv("CASSETTE_API_THINKING", raising=False)
    monkeypatch.delenv("CASSETTE_DEFAULT_THINKING_LEVEL", raising=False)
    assert ApiTransport._resolve_thinking_config({"model_selection": {"thinking_level": "max"}}) == "xhigh"


def test_api_resume_value_classifies_and_records_interrupts():
    t = ApiTransport()
    # A tool interrupt (editor_navigate) resumes KEYED by toolCall.id.
    rv, qs, needs = t._resume_value([{"id": "i1", "value": {"type": "tool", "toolCall": {"id": "call-9"}}}])
    assert needs is False and isinstance(rv, dict) and "call-9" in rv
    # A routine plan review is auto-approved with an audit record.
    rv, qs, needs = t._resume_value([{"id": "i2", "value": {"type": "edit_plan_review"}}])
    assert needs is False and rv == {"action": "approve"}
    assert qs and qs[0]["reason"] == "routine_plan_approval" and qs[0]["requires_user"] is False
    # A routine ask_user is auto-answered and the run continues (not halted).
    rv, qs, needs = t._resume_value([{"id": "i3", "value": {"type": "ask_user", "prompt": "which font looks best?"}}])
    assert needs is False and rv["action"] == "respond"


def test_worker_detached_path_routes_through_the_transport(cassette_env, monkeypatch):
    from cassette.core import worker

    monkeypatch.setattr(worker.notifier, "notify_terminal_job", lambda job: {"delivered": False})

    seen: dict = {}

    class _Recording:
        def run_job(self, job):
            seen["job_id"] = job.get("job_id")
            return _fake_result("api")

    monkeypatch.setattr(worker.transport, "get_transport", lambda: _Recording())

    jb = _make_job()
    out = worker.run(jb["job_id"])
    assert seen["job_id"] == jb["job_id"]
    assert out["status"] == "succeeded" and out["_via"] == "api"


def test_worker_resume_also_routes_through_the_transport(cassette_env, monkeypatch):
    from cassette.core import worker

    monkeypatch.setattr(worker.notifier, "notify_terminal_job", lambda job: {})

    seen: dict = {}

    class _Recording:
        def resume(self, job, response):
            seen["response"] = response
            return _fake_result("api-resume")

    monkeypatch.setattr(worker.transport, "get_transport", lambda: _Recording())

    jb = _make_job()
    jobs.update_job(jb["job_id"], resume_request={"response": "use the second take"})
    out = worker.run(jb["job_id"], action="resume")
    assert seen["response"] == "use the second take"
    assert out["_via"] == "api-resume"
