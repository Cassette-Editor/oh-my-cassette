from __future__ import annotations

import pytest

from cassette import jobs, notifier, tools, transport
from cassette.api_transport import ApiTransport
from cassette.transport import BrowserTransport, Transport, get_transport, selected_transport


@pytest.fixture(autouse=True)
def _isolate_hermes_env(monkeypatch, tmp_path):
    # Transport env resolution falls back to ~/.hermes/.env; point it at an absent file so these
    # hermetic tests never read the developer's real Hermes credentials.
    monkeypatch.setenv("HERMES_ENV_FILE", str(tmp_path / "absent.env"))


def test_default_transport_is_browser(monkeypatch):
    monkeypatch.delenv("CASSETTE_TRANSPORT", raising=False)
    assert selected_transport() == "browser"
    assert isinstance(get_transport(), BrowserTransport)


@pytest.mark.parametrize(
    "value,is_api",
    [("api", True), ("API", True), (" Api ", True), ("browser", False), ("", False), ("weird", False)],
)
def test_transport_selection_is_env_driven(monkeypatch, value, is_api):
    monkeypatch.setenv("CASSETTE_TRANSPORT", value)
    t = get_transport()
    assert isinstance(t, ApiTransport if is_api else BrowserTransport)


def test_both_transports_satisfy_protocol():
    assert isinstance(BrowserTransport(), Transport)
    assert isinstance(ApiTransport(), Transport)


def test_browser_transport_is_pure_passthrough(monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(tools.browser, "run_cassette_browser_job_threaded",
                        lambda job: {"status": "succeeded", "_via": "browser-run", "job_id": job.get("job_id")})
    monkeypatch.setattr(tools.browser, "export_reviewed_completion_job_threaded",
                        lambda job, decision: {"status": "succeeded", "_via": "browser-export", "decision": decision})
    monkeypatch.setattr(tools.browser, "close_browser_sessions_threaded",
                        lambda key=None: calls.__setitem__("close", key))
    monkeypatch.setattr(tools.browser, "check_playwright", lambda: True)

    bt = BrowserTransport()
    assert bt.run_job({"job_id": "j1"}) == {"status": "succeeded", "_via": "browser-run", "job_id": "j1"}
    assert bt.export({"job_id": "j1"}, {"decision": "export"})["decision"] == {"decision": "export"}
    bt.close_sessions("session-key")
    assert calls["close"] == "session-key"
    assert bt.check_available() is True


def test_check_playwright_tool_delegates_to_active_transport(monkeypatch):
    # tools.check_playwright is the readiness gate; under the API transport it reports API readiness
    # (the API origin defaults to the deployed Cassette, so readiness is credential-gated).
    monkeypatch.setenv("CASSETTE_TRANSPORT", "api")
    monkeypatch.delenv("CASSETTE_AUTH_EMAIL", raising=False)
    monkeypatch.delenv("CASSETTE_AUTH_ACCOUNT", raising=False)
    monkeypatch.delenv("CASSETTE_EMAIL", raising=False)
    monkeypatch.delenv("CASSETTE_AUTH_PASSWORD", raising=False)
    monkeypatch.delenv("CASSETTE_PASSWORD", raising=False)
    assert tools.check_playwright() is False
    monkeypatch.setenv("CASSETTE_AUTH_EMAIL", "e@x.io")
    monkeypatch.setenv("CASSETTE_AUTH_PASSWORD", "pw")
    assert tools.check_playwright() is True


def test_api_transport_availability_gating(monkeypatch):
    # The API origin defaults to the deployed Cassette, so availability is gated on credentials.
    for var in ("CASSETTE_AUTH_EMAIL", "CASSETTE_AUTH_ACCOUNT", "CASSETTE_EMAIL", "CASSETTE_AUTH_PASSWORD", "CASSETTE_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    assert ApiTransport().check_available() is False
    monkeypatch.setenv("CASSETTE_AUTH_EMAIL", "e@x.io")
    monkeypatch.setenv("CASSETTE_AUTH_PASSWORD", "pw")
    assert ApiTransport().check_available() is True
    monkeypatch.delenv("CASSETTE_AUTH_PASSWORD", raising=False)
    assert ApiTransport().check_available() is False


def test_api_transport_run_fails_clean_without_credentials(cassette_env, monkeypatch):
    monkeypatch.setenv("CASSETTE_TRANSPORT", "api")
    for var in ("CASSETTE_AUTH_EMAIL", "CASSETTE_AUTH_ACCOUNT", "CASSETTE_EMAIL", "CASSETTE_AUTH_PASSWORD", "CASSETTE_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    result = get_transport().run_job(
        {"job_id": "job-x", "session_hash": "s", "cassette_session_id": "s", "prompt": "edit", "asset_paths": []}
    )
    # Misconfiguration is a structured terminal failure, not a crash — same contract as the browser path.
    # No network is touched because credentials are validated before any request.
    assert result["status"] == "failed"
    assert set(result) >= {"status", "outputs", "questions", "errors", "quality", "final_screenshot"}
    assert result["errors"] and result["errors"][0]["code"] == "auth_missing_credentials"


def _make_job():
    return jobs.create_job(
        session_hash="sess",
        prompt="edit it",
        instruction=None,
        asset_paths=[],
        options={"cassette_session_id": "sess"},
    )


def _browser_shaped_succeeded(local_path: str) -> dict:
    return {
        "status": "succeeded",
        "outputs": [{"text": "out.mp4", "href": "/api/export/jobs/x/file",
                     "download": "out.mp4", "local_path": local_path, "kind": "video"}],
        "questions": [],
        "errors": [],
        "quality": {"completion_observed": True, "export_completed": True, "export_pending": False,
                    "output_link_count": 1, "local_output_count": 1, "risk": "low"},
        "final_screenshot": None,
    }


def test_result_dicts_share_the_same_contract_keys(cassette_env, tmp_path):
    mp4 = tmp_path / "out.mp4"
    mp4.write_bytes(b"video")
    browser_result = _browser_shaped_succeeded(str(mp4))
    api_result = ApiTransport()._result(
        "succeeded",
        outputs=[{"text": "out.mp4", "href": "/api/export/jobs/y/file",
                  "download": "out.mp4", "local_path": str(mp4), "kind": "video"}],
        completion_observed=True, export_completed=True, risk="low",
    )
    assert set(browser_result) == set(api_result)
    assert set(browser_result["quality"]) <= set(api_result["quality"]) or set(api_result["quality"]) <= set(browser_result["quality"])


def test_downstream_report_parity_browser_vs_api(cassette_env, tmp_path):
    mp4 = tmp_path / "out.mp4"
    mp4.write_bytes(b"video")

    jb = _make_job()
    jb.update(_browser_shaped_succeeded(str(mp4)))
    jb["status"] = "succeeded"

    ja = _make_job()
    ja.update(ApiTransport()._result(
        "succeeded",
        outputs=[{"text": "out.mp4", "href": "/api/export/jobs/y/file",
                  "download": "out.mp4", "local_path": str(mp4), "kind": "video"}],
        completion_observed=True, export_completed=True, risk="low",
    ))
    ja["status"] = "succeeded"

    scrubbed_b = tools._scrub_job(jb)
    scrubbed_a = tools._scrub_job(ja)
    # Equivalent outcomes must yield an identical user-facing report.
    for key in ("status", "user_summary", "output_count", "export_pending"):
        assert scrubbed_b["report"][key] == scrubbed_a["report"][key], key
    # Output scrubbing is identical: local_path stripped, downloaded+filename added.
    for scrubbed in (scrubbed_b, scrubbed_a):
        out = scrubbed["outputs"][0]
        assert "local_path" not in out
        assert out["downloaded"] is True
        assert out["filename"] == "out.mp4"


def test_notifier_delivery_parity_on_local_path(cassette_env, tmp_path):
    real = tmp_path / "v.mp4"
    real.write_bytes(b"video")
    missing = tmp_path / "missing.mp4"

    # A real on-disk export is delivered; a missing one is dropped — for either result shape.
    assert notifier._exported_media_paths({"outputs": [{"local_path": str(real), "kind": "video"}]}) == [str(real)]
    assert notifier._exported_media_paths({"outputs": [{"local_path": str(missing), "kind": "video"}]}) == []

    api_real = ApiTransport()._result(
        "succeeded",
        outputs=[{"text": "v", "href": "h", "download": "v", "local_path": str(real), "kind": "video"}],
    )
    assert notifier._exported_media_paths(api_real) == [str(real)]


def _fake_result(via: str) -> dict:
    return {"status": "succeeded", "_via": via, "outputs": [], "questions": [],
            "errors": [], "quality": {}, "final_screenshot": None}


@pytest.mark.parametrize("label,expected", [
    ("DeepSeek V4 Flash", "deepseek/deepseek-v4-flash"),
    ("DeepSeek V4 Pro", "deepseek/deepseek-v4-pro"),
    ("GPT-5.4 Mini", "openai/gpt-5.4-mini"),
    ("deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-pro"),  # already an id
    ("", "deepseek/deepseek-v4-flash"),                         # no choice -> default
])
def test_api_model_label_maps_to_id(label, expected):
    # The user's UI model *label* is honored (mapped to an agent model id), not dropped for the default.
    assert ApiTransport._resolve_model_id({"model_selection": {"model": label}}) == expected


def test_api_model_selection_required_fails_on_unmappable_label(monkeypatch):
    from cassette.api_transport import ApiTransportError
    monkeypatch.delenv("CASSETTE_REQUIRE_MODEL_SELECTION", raising=False)  # default true
    monkeypatch.delenv("CASSETTE_API_MODEL_ID", raising=False)
    with pytest.raises(ApiTransportError) as exc:
        ApiTransport._resolve_model_id({"model_selection": {"model": "Totally Unknown Model"}})
    assert exc.value.code == "model_selection_failed"
    monkeypatch.setenv("CASSETTE_REQUIRE_MODEL_SELECTION", "off")
    assert ApiTransport._resolve_model_id({"model_selection": {"model": "Totally Unknown Model"}}) == "deepseek/deepseek-v4-flash"


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


def test_worker_detached_path_routes_through_api_transport(cassette_env, monkeypatch):
    from cassette import worker
    monkeypatch.setenv("CASSETTE_TRANSPORT", "api")
    monkeypatch.setattr(worker.notifier, "notify_terminal_job", lambda job: {"delivered": False})

    seen: dict = {}

    class _Recording:
        def run_job(self, job):
            seen["job_id"] = job.get("job_id")
            return _fake_result("api")

    monkeypatch.setattr(worker.transport, "get_transport", lambda: _Recording())
    monkeypatch.setattr(worker.browser, "run_cassette_browser_job",
                        lambda job: (_ for _ in ()).throw(AssertionError("browser path ran under api transport")))

    jb = _make_job()
    out = worker.run(jb["job_id"])
    assert seen["job_id"] == jb["job_id"]
    assert out["status"] == "succeeded" and out["_via"] == "api"


def test_worker_detached_path_stays_on_browser_by_default(cassette_env, monkeypatch):
    from cassette import worker
    monkeypatch.delenv("CASSETTE_TRANSPORT", raising=False)
    monkeypatch.setattr(worker.notifier, "notify_terminal_job", lambda job: {})
    monkeypatch.setattr(worker.browser, "run_cassette_browser_job", lambda job: _fake_result("browser"))
    # The browser path must NOT construct the API transport at all.
    monkeypatch.setattr(worker.transport, "get_transport",
                        lambda: (_ for _ in ()).throw(AssertionError("api transport built on browser path")))

    jb = _make_job()
    out = worker.run(jb["job_id"])
    assert out["_via"] == "browser"
