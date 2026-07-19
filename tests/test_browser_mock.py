from __future__ import annotations

import os
from pathlib import Path
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from cassette import browser, jobs
from cassette import tools


def test_fetch_cassette_model_options_does_not_wait_for_browser_worker(monkeypatch):
    calls = []

    def fake_fetch(url=None, language="zh"):
        calls.append(browser._in_browser_worker())
        return {
            "models": [{"label": "Kimi K2.6"}],
            "thinking_levels": [{"label": "高", "value": "High"}],
            "source": "test",
            "language": language,
        }

    monkeypatch.setenv("CASSETTE_BROWSER_WORKER_THREAD", "true")
    monkeypatch.setattr(browser, "_fetch_cassette_model_options_direct", fake_fetch)

    browser._shutdown_browser_worker()
    browser._shutdown_model_options_worker()
    worker_started = threading.Event()
    release_worker = threading.Event()

    def block_browser_worker():
        worker_started.set()
        release_worker.wait(timeout=5)

    worker_future = browser._browser_worker().submit(block_browser_worker)
    assert worker_started.wait(timeout=1)
    started = time.monotonic()
    try:
        result = browser.fetch_cassette_model_options(language="zh")
    finally:
        release_worker.set()
        worker_future.result(timeout=2)
        browser._shutdown_browser_worker()
        browser._shutdown_model_options_worker()

    assert result["models"][0]["label"] == "Kimi K2.6"
    assert calls == [False]
    assert time.monotonic() - started < 1


def test_progress_summary_dedupes_assistant_reply():
    assistant_reply = (
        "已完成！以下是我所完成的内容： 导入了您的素材，时长 1 秒。 "
        "添加了字幕测试完成，覆盖完整时长。 视频总长度为 1 秒。"
    )
    summary = browser._summarize_page_state(assistant_reply)

    assert summary.count("已完成") == 1
    assert " | " not in summary
    assert "添加了字幕" in summary


def test_pending_routine_plan_is_not_completion_signal():
    text = (
        "I've reviewed your 13 clips. Ready to proceed with the edit. Shall I continue? "
        "Continue Stop Task Checklist 0/5 "
        "01 Read timeline and media context "
        "05 Final review and report completion"
    )

    assert browser._page_suggests_routine_interaction(text, "Shall I continue?") is True
    assert browser._chat_indicates_complete(text, "Shall I continue?") is False


def test_structured_state_completion_requires_export_ready():
    state = {
        "outputs": [],
        "routine_controls": [],
        "export_control": {"visible": True, "enabled": False},
        "assistant_checklist": None,
        "page_checklist": None,
        "assistant_routine_phrase": False,
        "assistant_completion_phrase": True,
        "page_completion_phrase": True,
    }

    assert browser._page_state_indicates_complete(state, export_required=True) is False
    assert browser._page_state_indicates_complete(state, export_required=False) is True

    state["export_control"]["enabled"] = True
    assert browser._page_state_indicates_complete(state, export_required=True) is True


def test_structured_state_routine_control_blocks_completion():
    state = {
        "outputs": [],
        "routine_controls": [{"text": "Continue"}],
        "export_control": {"visible": True, "enabled": True},
        "assistant_checklist": None,
        "page_checklist": {"done": 0, "total": 5},
        "assistant_routine_phrase": True,
        "assistant_completion_phrase": True,
        "page_completion_phrase": True,
    }

    assert browser._page_state_indicates_routine_interaction(state) is True
    assert browser._page_state_indicates_complete(state, export_required=True) is False


def test_structural_export_ready_requires_hermes_review_without_semantic_confirmation():
    state = {
        "assistant_text": "I made the requested timeline changes and the export control is ready.",
        "outputs": [],
        "routine_controls": [],
        "export_control": {"visible": True, "enabled": True, "label": "Export"},
        "stop_control": {"visible": False, "enabled": False, "label": ""},
        "assistant_checklist": None,
        "page_checklist": None,
        "assistant_routine_phrase": False,
        "assistant_completion_phrase": False,
        "page_completion_phrase": False,
        "assistant_is_current": True,
        "current_response_observed": True,
        "page_completion_allowed": False,
    }

    assert browser._page_state_indicates_complete(state, export_required=True) is False
    assert browser._page_state_requires_completion_review(state, export_required=True) is True

    state["stop_control"] = {"visible": True, "enabled": True, "label": "Stop"}
    assert browser._page_state_requires_completion_review(state, export_required=True) is False
    assert browser._page_state_indicates_complete(state, export_required=True) is False


def test_cassette_negative_reply_requires_hermes_review_even_if_export_button_enabled():
    state = {
        "assistant_text": "I could not complete the edit because the media failed to process.",
        "outputs": [],
        "routine_controls": [],
        "export_control": {"visible": True, "enabled": True, "label": "Export"},
        "stop_control": {"visible": False, "enabled": False, "label": ""},
        "assistant_checklist": None,
        "page_checklist": None,
        "assistant_routine_phrase": False,
        "assistant_completion_phrase": False,
        "assistant_completion_denial": True,
        "page_completion_phrase": False,
        "assistant_is_current": True,
        "current_response_observed": True,
        "page_completion_allowed": False,
    }

    assert browser._page_state_reports_incomplete(state) is True
    assert browser._page_state_indicates_complete(state, export_required=True) is False
    assert browser._page_state_requires_completion_review(state, export_required=True) is True


def test_cassette_negative_reply_requires_hermes_review_before_timeout():
    state = {
        "assistant_text": "I could not complete the edit because the media failed to process.",
        "outputs": [],
        "routine_controls": [],
        "export_control": {"visible": True, "enabled": False, "label": "Export"},
        "stop_control": {"visible": False, "enabled": False, "label": ""},
        "assistant_checklist": None,
        "page_checklist": None,
        "assistant_routine_phrase": False,
        "assistant_completion_phrase": False,
        "assistant_completion_denial": True,
        "page_completion_phrase": False,
        "assistant_is_current": True,
        "current_response_observed": True,
        "page_completion_allowed": False,
    }

    assert browser._page_state_indicates_complete(state, export_required=True) is False
    assert browser._page_state_requires_completion_review(state, export_required=True) is True


def test_ambiguous_idle_reply_requires_hermes_review_without_keyword_match():
    state = {
        "assistant_text": "I adjusted the timeline and left the current draft in the editor.",
        "outputs": [],
        "routine_controls": [],
        "export_control": {"visible": True, "enabled": False, "label": "Export"},
        "stop_control": {"visible": False, "enabled": False, "label": ""},
        "assistant_checklist": None,
        "page_checklist": None,
        "assistant_routine_phrase": False,
        "assistant_completion_phrase": False,
        "assistant_completion_denial": False,
        "page_completion_phrase": False,
        "assistant_is_current": True,
        "current_response_observed": True,
        "page_completion_allowed": False,
    }

    assert browser._page_state_reports_incomplete(state) is False
    assert browser._page_state_indicates_complete(state, export_required=True) is False
    assert browser._page_state_requires_completion_review(state, export_required=True) is True


def test_explicit_completion_still_allows_export_in_any_language():
    state = {
        "outputs": [],
        "routine_controls": [],
        "export_control": {"visible": True, "enabled": True, "label": "Export"},
        "stop_control": {"visible": False, "enabled": False, "label": ""},
        "assistant_checklist": None,
        "page_checklist": None,
        "assistant_routine_phrase": False,
        "assistant_completion_phrase": True,
        "assistant_completion_denial": False,
        "page_completion_phrase": False,
        "assistant_is_current": True,
        "current_response_observed": True,
        "page_completion_allowed": False,
    }

    assert browser._page_state_indicates_complete(state, export_required=True) is True
    assert browser._page_state_requires_completion_review(state, export_required=True) is False


def test_completed_reply_with_feature_limitation_still_allows_export():
    text = (
        "剪辑完成，可以导出。已根据请求完成以下修改：暗红色调、降低原声、高潮变红。"
        "注意：当前编辑器无音频EQ工具，中低频增强无法实现；建议导出后处理音频。"
    )
    state = {
        "assistant_text": text,
        "outputs": [],
        "routine_controls": [],
        "export_control": {"visible": True, "enabled": True, "label": "导出"},
        "stop_control": {"visible": False, "enabled": False, "label": ""},
        "assistant_checklist": None,
        "page_checklist": None,
        "assistant_routine_phrase": False,
        "assistant_completion_phrase": browser._completion_phrase(text),
        "assistant_completion_denial": browser._completion_denial_phrase(text),
        "page_completion_phrase": False,
        "assistant_is_current": True,
        "current_response_observed": True,
        "page_completion_allowed": False,
    }

    assert browser._completion_denial_phrase(text) is False
    assert browser._page_state_reports_incomplete(state) is False
    assert browser._page_state_indicates_complete(state, export_required=True) is True


def test_completed_reply_with_worker_report_block_still_allows_export():
    text = (
        "所有编辑操作已完成：猫猫视频已导入主轨道，BGM高潮已对齐视频，"
        "原视频音量已降低，已添加抽象中文文案。"
        "但由于worker_report连续被blocked状态阻塞，无法通过worker_report正式报告完成。"
    )
    state = {
        "assistant_text": text,
        "outputs": [],
        "routine_controls": [],
        "export_control": {"visible": True, "enabled": True, "label": "导出"},
        "stop_control": {"visible": False, "enabled": False, "label": ""},
        "assistant_checklist": None,
        "page_checklist": None,
        "assistant_routine_phrase": False,
        "assistant_completion_phrase": browser._completion_phrase(text),
        "assistant_completion_denial": browser._completion_denial_phrase(text),
        "page_completion_phrase": False,
        "assistant_is_current": True,
        "current_response_observed": True,
        "page_completion_allowed": False,
    }

    assert browser._completion_phrase(text) is True
    assert browser._completion_denial_phrase(text) is False
    assert browser._page_state_reports_incomplete(state) is False
    assert browser._page_state_indicates_complete(state, export_required=True) is True


def test_upload_status_parses_chinese_ready_and_failure():
    assert browser._upload_status_counts("1 个就绪，0 个失败") == (1, 0)
    assert browser._upload_status_counts("就绪: 1 个，失败: 0 个") == (1, 0)
    assert browser._upload_status_ready_for_expected("1 个就绪，0 个失败", 1) is True
    assert browser._upload_status_ready_for_expected("4 个就绪，0 个失败", 6) is False
    assert browser._upload_status_has_failure("0 个就绪，1 个失败") is True
    assert browser._upload_status_ready_for_expected("1 个进行中 · 100%", 1) is False
    assert browser._upload_status_ready_for_expected("1 active", 1) is False


def test_default_chat_selectors_prefer_current_remotion_testids():
    assert browser.DEFAULT_CHAT_SELECTOR.split(",", 1)[0] == "[data-testid^='chat-input-textarea-']"
    assert browser.DEFAULT_SEND_SELECTOR.split(",", 1)[0] == "[data-testid^='chat-input-send-']"
    assert (
        "[data-testid^='chat-input-textarea-']:visible"
        in browser._chat_input_candidates(browser.DEFAULT_CHAT_SELECTOR)[:2]
    )


def test_upload_ready_expected_count_tracks_upload_batch_files():
    assert browser._upload_ready_expected_count(["clip.mp4", "bgm.mp3"]) == 2
    assert browser._upload_ready_expected_count(["clip.mp4", "still.jpg", "bgm.wav"]) == 3
    assert browser._upload_ready_expected_count(["voice.mp3"]) == 1
    assert browser._upload_ready_expected_count([]) == 0


def test_asset_paths_needing_upload_returns_incremental_batch(cassette_env):
    first = cassette_env["source_root"] / "clip.mp4"
    second = cassette_env["source_root"] / "clip-2.mp4"
    first.write_bytes(b"video")
    second.write_bytes(b"video 2")
    record = {"uploaded_asset_fingerprints": browser._asset_fingerprint([str(first)])}

    assert browser._asset_paths_needing_upload(record, [str(first), str(second)]) == [str(second)]


def test_mark_uploaded_assets_preserves_full_current_fingerprint(cassette_env):
    first = cassette_env["source_root"] / "clip.mp4"
    second = cassette_env["source_root"] / "clip-2.mp4"
    first.write_bytes(b"video")
    second.write_bytes(b"video 2")
    record = {"uploaded_asset_fingerprints": browser._asset_fingerprint([str(first)])}

    browser._mark_uploaded_assets(record, [str(second)], [str(first), str(second)])

    assert set(record["uploaded_asset_fingerprints"]) == set(browser._asset_fingerprint([str(first), str(second)]))
    assert record["asset_fingerprint"] == browser._asset_fingerprint([str(first), str(second)])


def test_chinese_routine_and_completion_phrases_are_classified():
    routine = "执行计划 0/5 待确认。请确认是否继续执行。"
    complete = "任务完成，剪辑完成，可以导出。任务清单 5/5"

    assert browser._page_suggests_routine_interaction(routine, routine) is True
    assert browser._chat_indicates_complete(routine, routine) is False
    assert browser._chat_indicates_complete(complete, complete) is True
    assert browser._thinking_level_ui_candidates("Medium")[:2] == ["Medium", "中"]


def test_completion_denial_phrases_are_classified_in_chinese_and_english():
    assert (
        browser._completion_denial_phrase("I couldn't complete the edit because the media failed to process.") is True
    )
    assert browser._completion_denial_phrase("我无法完成剪辑，因为素材处理失败。") is True
    assert (
        browser._completion_denial_phrase(
            "剪辑完成，可以导出。当前编辑器无音频EQ工具，中低频增强无法实现；建议导出后处理音频。"
        )
        is False
    )
    assert (
        browser._completion_denial_phrase(
            "所有编辑操作已完成，但worker_report被阻塞，无法通过worker_report正式报告完成。"
        )
        is False
    )
    assert browser._completion_denial_phrase("The edit is complete and ready to export.") is False


@pytest.mark.skipif(not browser.check_playwright(), reason="playwright is not installed")
def test_browser_mock_completes(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    fixture = Path(__file__).parent / "fixtures" / "cassette_mock.html"
    job = jobs.create_job(
        "sess",
        "Make a short edit",
        "instruction",
        [str(media)],
        {"url": fixture.resolve().as_uri(), "timeout_sec": 10},
    )
    result = browser.run_cassette_browser_job(job)
    assert result["status"] == "succeeded"
    assert result["outputs"][0]["download"] == "output.mp4"
    assert Path(result["outputs"][0]["local_path"]).exists()
    assert result["final_screenshot"]


@pytest.mark.skipif(not browser.check_playwright(), reason="playwright is not installed")
def test_browser_chinese_agent_ui_completes(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    fixture = Path(__file__).parent / "fixtures" / "cassette_zh_mock.html"
    job = jobs.create_job(
        "sess",
        "Make a short edit",
        "instruction",
        [str(media)],
        {
            "url": fixture.resolve().as_uri(),
            "timeout_sec": 10,
            "chat_message": "请剪成 10 秒短视频，加中文字幕",
            "model_selection": {"model": "Kimi K2.6", "thinking_level": "Medium"},
        },
    )
    result = browser.run_cassette_browser_job(job)
    assert result["status"] == "succeeded"
    assert result["outputs"][0]["download"] == "zh-output.mp4"
    assert Path(result["outputs"][0]["local_path"]).exists()


@pytest.mark.skipif(not browser.check_playwright(), reason="playwright is not installed")
def test_browser_waits_for_agent_ui_ready_after_page_load(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    fixture = Path(__file__).parent / "fixtures" / "cassette_delayed_ui_mock.html"
    job = jobs.create_job(
        "delayed-ui",
        "Make a short edit",
        "instruction",
        [str(media)],
        {
            "url": fixture.resolve().as_uri(),
            "timeout_sec": 10,
            "chat_message": "Make a short video.",
            "cassette_language": "en",
        },
    )

    result = browser.run_cassette_browser_job(job)

    assert result["status"] == "succeeded"
    persisted = jobs.load_job(job["job_id"])
    assert any(
        event.get("stage") == "ui_ready" and event.get("operation_status") == "succeeded"
        for event in persisted.get("browser_events", [])
    )


@pytest.mark.skipif(not browser.check_playwright(), reason="playwright is not installed")
def test_browser_authenticates_from_hermes_env_before_upload(cassette_env):
    hermes_home = Path(os.environ["HERMES_HOME"])
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / ".env").write_text(
        "CASSETTE_AUTH_EMAIL=operator@example.com\nCASSETTE_AUTH_PASSWORD=generated-password-1234\n",
        encoding="utf-8",
    )
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    fixture = Path(__file__).parent / "fixtures" / "cassette_auth_mock.html"
    job = jobs.create_job(
        "sess",
        "Make a short edit",
        "instruction",
        [str(media)],
        {"url": fixture.resolve().as_uri(), "timeout_sec": 10, "chat_message": "Make a short edit"},
    )

    result = browser.run_cassette_browser_job(job)

    assert result["status"] == "succeeded"
    assert result["outputs"][0]["download"] == "auth-output.mp4"
    persisted = jobs.load_job(job["job_id"])
    browser_events = persisted.get("browser_events") or []
    assert any(event.get("stage") == "connectivity" for event in browser_events)
    assert any(
        event.get("stage") == "authentication" and event.get("operation_status") == "authenticated"
        for event in browser_events
    )
    serialized_events = str(browser_events)
    assert "operator@example.com" not in serialized_events
    assert "generated-password-1234" not in serialized_events


@pytest.mark.skipif(not browser.check_playwright(), reason="playwright is not installed")
def test_browser_authenticates_when_auth_form_is_delayed(cassette_env):
    hermes_home = Path(os.environ["HERMES_HOME"])
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / ".env").write_text(
        "CASSETTE_AUTH_EMAIL=operator@example.com\nCASSETTE_AUTH_PASSWORD=generated-password-1234\n",
        encoding="utf-8",
    )
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    fixture = Path(__file__).parent / "fixtures" / "cassette_delayed_auth_mock.html"
    job = jobs.create_job(
        "sess",
        "Make a short edit",
        "instruction",
        [str(media)],
        {"url": fixture.resolve().as_uri(), "timeout_sec": 12, "chat_message": "Make a short edit"},
    )

    result = browser.run_cassette_browser_job(job)

    assert result["status"] == "succeeded"
    assert result["outputs"][0]["download"] == "delayed-auth-output.mp4"
    browser_events = jobs.load_job(job["job_id"]).get("browser_events") or []
    assert any(
        event.get("stage") == "authentication" and event.get("operation_status") == "authenticated"
        for event in browser_events
    )


@pytest.mark.skipif(not browser.check_playwright(), reason="playwright is not installed")
def test_browser_authenticates_with_enter_without_clicking_submit(cassette_env):
    hermes_home = Path(os.environ["HERMES_HOME"])
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / ".env").write_text(
        "CASSETTE_AUTH_EMAIL=operator@example.com\nCASSETTE_AUTH_PASSWORD=generated-password-1234\n",
        encoding="utf-8",
    )
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    fixture = Path(__file__).parent / "fixtures" / "cassette_auth_submit_timeout_mock.html"
    job = jobs.create_job(
        "sess",
        "Make a short edit",
        "instruction",
        [str(media)],
        {"url": fixture.resolve().as_uri(), "timeout_sec": 12, "chat_message": "Make a short edit"},
    )

    result = browser.run_cassette_browser_job(job)

    assert result["status"] == "succeeded"
    assert result["outputs"][0]["download"] == "submit-timeout-auth-output.mp4"
    browser_events = jobs.load_job(job["job_id"]).get("browser_events") or []
    assert any(
        event.get("stage") == "authentication" and event.get("operation_status") == "authenticated"
        for event in browser_events
    )


@pytest.mark.skipif(not browser.check_playwright(), reason="playwright is not installed")
def test_browser_auth_missing_credentials_fails_before_upload(cassette_env):
    browser.close_browser_sessions()
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    fixture = Path(__file__).parent / "fixtures" / "cassette_auth_mock.html"
    job = jobs.create_job(
        "sess",
        "Make a short edit",
        "instruction",
        [str(media)],
        {"url": fixture.resolve().as_uri(), "timeout_sec": 10, "chat_message": "Make a short edit"},
    )

    result = browser.run_cassette_browser_job(job)

    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "cassette_auth_missing_credentials"
    browser_events = jobs.load_job(job["job_id"]).get("browser_events") or []
    assert any(
        event.get("stage") == "authentication" and event.get("operation_status") == "failed" for event in browser_events
    )


@pytest.mark.skipif(not browser.check_playwright(), reason="playwright is not installed")
def test_browser_chat_completion_exports_downloaded_file(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    fixture = Path(__file__).parent / "fixtures" / "cassette_chat_complete_mock.html"
    job = jobs.create_job(
        "sess",
        "Make a short edit",
        "instruction",
        [str(media)],
        {"url": fixture.resolve().as_uri(), "timeout_sec": 10},
    )
    result = browser.run_cassette_browser_job(job)
    assert result["status"] == "succeeded"
    assert result["outputs"][0]["download"] == "chat-complete-output.mp4"
    assert Path(result["outputs"][0]["local_path"]).exists()
    assert result["quality"]["completion_source"] == "cassette_chat_panel"
    assert result["quality"]["export_completed"] is True
    assert result["quality"]["export_pending"] is False


@pytest.mark.skipif(not browser.check_playwright(), reason="playwright is not installed")
def test_browser_sends_chat_message_not_internal_prompt(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    fixture = Path(__file__).parent / "fixtures" / "cassette_chat_echo_mock.html"
    job = jobs.create_job(
        "sess",
        "INTERNAL HERMES PROMPT SHOULD NOT BE SENT",
        "instruction",
        [str(media)],
        {
            "url": fixture.resolve().as_uri(),
            "timeout_sec": 10,
            "chat_message": "请剪成 10 秒短视频，加中文字幕",
        },
    )
    result = browser.run_cassette_browser_job(job)
    assert result["status"] == "succeeded"
    assert "请剪成 10 秒短视频" in result["quality"]["progress_summary"]
    assert "INTERNAL HERMES PROMPT" not in result["quality"]["progress_summary"]


@pytest.mark.skipif(not browser.check_playwright(), reason="playwright is not installed")
def test_browser_sends_chat_with_icon_only_composer_button(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    fixture = Path(__file__).parent / "fixtures" / "cassette_icon_send_mock.html"
    job = jobs.create_job(
        "sess",
        "Make a short edit",
        "instruction",
        [str(media)],
        {
            "url": fixture.resolve().as_uri(),
            "timeout_sec": 10,
            "chat_message": "Make a 5 second edit",
        },
    )
    result = browser.run_cassette_browser_job(job)
    assert result["status"] == "succeeded"
    assert result["outputs"][0]["download"] == "icon-send-output.mp4"


@pytest.mark.skipif(not browser.check_playwright(), reason="playwright is not installed")
def test_browser_waits_for_agent_upload_ready_before_chat(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    fixture = Path(__file__).parent / "fixtures" / "cassette_agent_upload_ready_mock.html"
    job = jobs.create_job(
        "sess",
        "Make a short edit",
        "instruction",
        [str(media)],
        {
            "url": fixture.resolve().as_uri(),
            "timeout_sec": 10,
            "chat_message": "请剪成 10 秒短视频",
        },
    )
    result = browser.run_cassette_browser_job(job)
    assert result["status"] == "succeeded"
    assert "sent before upload ready" not in result["quality"]["progress_summary"]


@pytest.mark.skipif(not browser.check_playwright(), reason="playwright is not installed")
def test_browser_approves_routine_cassette_plan_without_user(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    fixture = Path(__file__).parent / "fixtures" / "cassette_plan_approval_mock.html"
    job = jobs.create_job(
        "sess",
        "Make a short edit",
        "instruction",
        [str(media)],
        {
            "url": fixture.resolve().as_uri(),
            "timeout_sec": 10,
            "chat_message": "请剪成 10 秒短视频",
        },
    )
    result = browser.run_cassette_browser_job(job)

    assert result["status"] == "succeeded"
    assert result["outputs"][0]["download"] == "plan-approved-output.mp4"
    assert result["questions"][0]["requires_user"] is False
    assert result["questions"][0]["reason"] == "routine_plan_approval"
    assert "Approve plan" in result["questions"][0]["answer"]
    assert result["questions"][0]["answer"] != browser.ROUTINE_PLAN_APPROVAL


def test_upload_wait_can_continue_past_180_seconds(monkeypatch):
    clock = {"now": 0.0}

    class FakeLocator:
        @property
        def first(self):
            return self

        def inner_text(self, timeout=0):
            if clock["now"] <= 180:
                return "Processing 100%"
            return "4 ready, 0 failed"

    class FakePage:
        def locator(self, selector):
            return FakeLocator()

    monkeypatch.setattr(browser.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(browser.time, "sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))

    body = browser._wait_for_agent_upload_ready(FakePage(), "job", 4, timeout_sec=240)

    assert clock["now"] > 180
    assert "4 ready, 0 failed" in body


def test_upload_timeout_defaults_to_job_timeout(monkeypatch):
    monkeypatch.delenv("CASSETTE_UPLOAD_TIMEOUT_SEC", raising=False)

    assert browser._upload_timeout_sec({"timeout_sec": 37}) == 37
    assert browser._upload_timeout_sec({}) == 1800


def test_upload_timeout_can_be_disabled_explicitly(monkeypatch):
    monkeypatch.setenv("CASSETTE_UPLOAD_TIMEOUT_SEC", "0")

    assert browser._upload_timeout_sec({"timeout_sec": 37}) is None


def test_upload_wait_raises_specific_timeout(monkeypatch):
    clock = {"now": 0.0}

    class FakeLocator:
        @property
        def first(self):
            return self

        def inner_text(self, timeout=0):
            return "1 个进行中"

    class FakePage:
        def locator(self, selector):
            return FakeLocator()

    monkeypatch.setattr(browser.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(browser.time, "sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))

    with pytest.raises(browser.BrowserUploadTimeoutError) as exc_info:
        browser._wait_for_agent_upload_ready(FakePage(), "job", 1, timeout_sec=2)
    assert clock["now"] >= 2
    assert "last upload status: 1 个进行中" in str(exc_info.value)


def test_upload_wait_accepts_chinese_ready_status(monkeypatch):
    class FakeLocator:
        @property
        def first(self):
            return self

        def inner_text(self, timeout=0):
            return "4 个就绪，0 个失败"

    class FakePage:
        def locator(self, selector):
            return FakeLocator()

    body = browser._wait_for_agent_upload_ready(FakePage(), "job", 4, timeout_sec=1)

    assert "4 个就绪，0 个失败" in body


def test_upload_wait_requires_expected_ready_count(monkeypatch):
    clock = {"now": 0.0}

    class FakeLocator:
        @property
        def first(self):
            return self

        def inner_text(self, timeout=0):
            return "4 个就绪，0 个失败"

    class FakePage:
        def locator(self, selector):
            return FakeLocator()

    monkeypatch.setattr(browser.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(browser.time, "sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))

    with pytest.raises(RuntimeError, match="Timed out waiting"):
        browser._wait_for_agent_upload_ready(FakePage(), "job", 6, timeout_sec=2)


def test_upload_wait_fails_only_on_cassette_failed_status(monkeypatch):
    class FakeLocator:
        @property
        def first(self):
            return self

        def inner_text(self, timeout=0):
            return "3 ready, 1 failed"

    class FakePage:
        def locator(self, selector):
            return FakeLocator()

    with pytest.raises(RuntimeError, match="upload/analysis failed"):
        browser._wait_for_agent_upload_ready(FakePage(), "job", 4, timeout_sec=None)


def test_upload_wait_fails_on_chinese_failed_status(monkeypatch):
    class FakeLocator:
        @property
        def first(self):
            return self

        def inner_text(self, timeout=0):
            return "3 个就绪，1 个失败"

    class FakePage:
        def locator(self, selector):
            return FakeLocator()

    with pytest.raises(RuntimeError, match="upload/analysis failed"):
        browser._wait_for_agent_upload_ready(FakePage(), "job", 4, timeout_sec=None)


def test_live_page_ready_assets_allow_upload_skip():
    class FakeLocator:
        @property
        def first(self):
            return self

        def inner_text(self, timeout=0):
            return "13 ready, 0 failed"

    class FakePage:
        def locator(self, selector):
            return FakeLocator()

    assert browser._agent_page_has_ready_assets(FakePage(), 13) is True
    assert browser._agent_page_has_ready_assets(FakePage(), 14) is False


def test_live_page_ready_assets_accepts_chinese_status():
    class FakeLocator:
        @property
        def first(self):
            return self

        def inner_text(self, timeout=0):
            return "13 个就绪，0 个失败"

    class FakePage:
        def locator(self, selector):
            return FakeLocator()

    assert browser._agent_page_has_ready_assets(FakePage(), 13) is True
    assert browser._agent_page_has_ready_assets(FakePage(), 14) is False


def test_live_page_ready_assets_requires_upload_status_not_body_text():
    class FakeLocator:
        def __init__(self, selector):
            self.selector = selector

        @property
        def first(self):
            return self

        def inner_text(self, timeout=0):
            if "agent-upload-status" in self.selector:
                return ""
            return "The edit is complete and ready to export."

    class FakePage:
        def locator(self, selector):
            return FakeLocator(selector)

    assert browser._agent_page_has_ready_assets(FakePage(), 1) is False


def test_live_page_ready_assets_prefers_structured_upload_state():
    class FakeLocator:
        @property
        def first(self):
            return self

        def inner_text(self, timeout=0):
            return "6 ready, 0 failed"

    class FakePage:
        def __init__(self, state):
            self.state = state

        def evaluate(self, script):
            return self.state

        def locator(self, selector):
            return FakeLocator()

    partial = {
        "source": "upload-strip",
        "total": "6",
        "completed": "4",
        "failed": "0",
        "active": "2",
        "status": "active",
    }
    ready = {
        "source": "upload-strip",
        "total": "6",
        "completed": "6",
        "failed": "0",
        "active": "0",
        "status": "ready",
    }

    assert browser._agent_page_has_ready_assets(FakePage(partial), 6) is False
    assert browser._agent_page_has_ready_assets(FakePage(ready), 6) is True


def test_upload_wait_uses_structured_state_before_status_text(monkeypatch):
    clock = {"now": 0.0}
    states = [
        {
            "source": "upload-strip",
            "total": "6",
            "completed": "4",
            "failed": "0",
            "active": "2",
            "status": "active",
            "statusText": "6 ready, 0 failed",
        },
        {
            "source": "upload-strip",
            "total": "6",
            "completed": "6",
            "failed": "0",
            "active": "0",
            "status": "ready",
            "statusText": "6 ready, 0 failed",
        },
    ]

    class FakeLocator:
        @property
        def first(self):
            return self

        def inner_text(self, timeout=0):
            return "6 ready, 0 failed"

    class FakePage:
        def locator(self, selector):
            return FakeLocator()

        def evaluate(self, script):
            return states[min(int(clock["now"]), 1)]

    monkeypatch.setattr(browser.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(browser.time, "sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))

    body = browser._wait_for_agent_upload_ready(FakePage(), "job", 6, timeout_sec=3)

    assert clock["now"] == 1.0
    assert "6 ready, 0 failed" in body


def test_upload_wait_fails_on_structured_failed_state(monkeypatch):
    class FakeLocator:
        @property
        def first(self):
            return self

        def inner_text(self, timeout=0):
            return "6 ready, 0 failed"

    class FakePage:
        def locator(self, selector):
            return FakeLocator()

        def evaluate(self, script):
            return {
                "source": "upload-strip",
                "total": "6",
                "completed": "5",
                "failed": "1",
                "active": "0",
                "status": "failed",
                "statusText": "6 ready, 0 failed",
            }

    with pytest.raises(RuntimeError, match="upload/analysis failed"):
        browser._wait_for_agent_upload_ready(FakePage(), "job", 6, timeout_sec=None)


def test_reused_model_selection_must_match_requested_model():
    record = {"model_selection": {"model": "Kimi K2.6", "thinking_level": "Medium", "status": "selected"}}

    assert (
        browser._record_model_selection_matches(
            record,
            {"model_selection": {"model": "Kimi K2.6", "thinking_level": "Medium"}},
        )
        is True
    )
    assert (
        browser._record_model_selection_matches(
            record,
            {"model_selection": {"model": "DeepSeek", "thinking_level": "Medium"}},
        )
        is False
    )
    assert (
        browser._record_model_selection_matches(
            record,
            {"model_selection": {"model": "Kimi K2.6", "thinking_level": "High"}},
        )
        is False
    )


@pytest.mark.skipif(not browser.check_playwright(), reason="playwright is not installed")
def test_same_session_reuses_uploaded_assets_without_reupload(cassette_env, monkeypatch):
    browser.close_browser_sessions()
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    fixture = Path(__file__).parent / "fixtures" / "cassette_chat_complete_mock.html"
    original_upload = browser._upload_assets
    original_select_model = browser._select_cassette_model
    upload_calls = []
    model_selection_calls = []

    def counted_upload(page, asset_paths, upload_selector):
        upload_calls.append(list(asset_paths))
        return original_upload(page, asset_paths, upload_selector)

    def counted_select_model(page, job):
        model_selection_calls.append(job["job_id"])
        return original_select_model(page, job)

    monkeypatch.setattr(browser, "_upload_assets", counted_upload)
    monkeypatch.setattr(browser, "_select_cassette_model", counted_select_model)
    try:
        first = jobs.create_job(
            "same-session",
            "Make a short edit",
            "instruction",
            [str(media)],
            {"url": fixture.resolve().as_uri(), "timeout_sec": 10, "cassette_session_id": "gateway-session"},
        )
        second = jobs.create_job(
            "same-session",
            "Make another short edit",
            "instruction",
            [str(media)],
            {"url": fixture.resolve().as_uri(), "timeout_sec": 10, "cassette_session_id": "gateway-session"},
        )

        first_result = browser.run_cassette_browser_job(first)
        second_result = browser.run_cassette_browser_job(second)

        assert first_result["status"] == "succeeded"
        assert second_result["status"] == "succeeded"
        assert len(upload_calls) == 1
        assert len(model_selection_calls) == 1
        assert second_result["quality"]["stage_timings"]["model_selection"]["status"] == "skipped"
        assert jobs.load_job(second["job_id"])["model_selection"]["reason"] == "session_reuse"
    finally:
        browser.close_browser_sessions()


@pytest.mark.skipif(not browser.check_playwright(), reason="playwright is not installed")
def test_same_session_ignores_new_query_and_reuses_page(cassette_env, monkeypatch):
    browser.close_browser_sessions()
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    fixture = Path(__file__).parent / "fixtures" / "cassette_chat_complete_mock.html"
    upload_calls = []
    original_upload = browser._upload_assets

    def counted_upload(page, asset_paths, upload_selector):
        upload_calls.append(list(asset_paths))
        return original_upload(page, asset_paths, upload_selector)

    monkeypatch.setattr(browser, "_upload_assets", counted_upload)
    try:
        first = jobs.create_job(
            "same-session-new-query",
            "Make a short edit",
            "instruction",
            [str(media)],
            {"url": fixture.resolve().as_uri(), "timeout_sec": 10, "cassette_session_id": "gateway-session-query"},
        )
        second = jobs.create_job(
            "same-session-new-query",
            "Make another short edit",
            "instruction",
            [str(media)],
            {
                "url": f"{fixture.resolve().as_uri()}?new=true",
                "timeout_sec": 10,
                "cassette_session_id": "gateway-session-query",
            },
        )

        assert browser.run_cassette_browser_job(first)["status"] == "succeeded"
        assert browser.run_cassette_browser_job(second)["status"] == "succeeded"
        assert len(upload_calls) == 1
    finally:
        browser.close_browser_sessions()


@pytest.mark.skipif(not browser.check_playwright(), reason="playwright is not installed")
def test_same_session_followup_waits_for_new_assistant_before_export(cassette_env):
    browser.close_browser_sessions()
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    fixture = Path(__file__).parent / "fixtures" / "cassette_reused_chat_delayed_mock.html"
    try:
        first = jobs.create_job(
            "same-session-delayed",
            "Make a short edit",
            "instruction",
            [str(media)],
            {"url": fixture.resolve().as_uri(), "timeout_sec": 10, "cassette_session_id": "gateway-session-delayed"},
        )
        second = jobs.create_job(
            "same-session-delayed",
            "Make the BGM louder",
            "instruction",
            [str(media)],
            {"url": fixture.resolve().as_uri(), "timeout_sec": 10, "cassette_session_id": "gateway-session-delayed"},
        )

        first_result = browser.run_cassette_browser_job(first)
        second_result = browser.run_cassette_browser_job(second)

        assert first_result["status"] == "succeeded"
        assert first_result["outputs"][0]["download"] == "first-output.mp4"
        assert second_result["status"] == "succeeded"
        assert second_result["outputs"][0]["download"] == "second-output.mp4"
    finally:
        browser.close_browser_sessions()


@pytest.mark.skipif(not browser.check_playwright(), reason="playwright is not installed")
def test_same_session_new_assets_uploads_only_incremental_files(cassette_env, monkeypatch):
    browser.close_browser_sessions()
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    second_media = cassette_env["source_root"] / "clip-2.mp4"
    second_media.write_bytes(b"video 2")
    fixture = Path(__file__).parent / "fixtures" / "cassette_chat_complete_mock.html"
    upload_calls = []
    original_upload = browser._upload_assets

    def counted_upload(page, asset_paths, upload_selector):
        upload_calls.append(list(asset_paths))
        return original_upload(page, asset_paths, upload_selector)

    monkeypatch.setattr(browser, "_upload_assets", counted_upload)
    try:
        first = jobs.create_job(
            "same-session-new-assets",
            "Make a short edit",
            "instruction",
            [str(media)],
            {"url": fixture.resolve().as_uri(), "timeout_sec": 10, "cassette_session_id": "gateway-session-assets"},
        )
        second = jobs.create_job(
            "same-session-new-assets",
            "Make another short edit",
            "instruction",
            [str(media), str(second_media)],
            {"url": fixture.resolve().as_uri(), "timeout_sec": 10, "cassette_session_id": "gateway-session-assets"},
        )

        assert browser.run_cassette_browser_job(first)["status"] == "succeeded"
        assert browser.run_cassette_browser_job(second)["status"] == "succeeded"
        assert len(upload_calls) == 2
        assert upload_calls[1] == [str(second_media)]
    finally:
        browser.close_browser_sessions()


@pytest.mark.skipif(not browser.check_playwright(), reason="playwright is not installed")
def test_agent_error_text_requires_hermes_review_before_job_timeout(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    fixture = Path(__file__).parent / "fixtures" / "cassette_agent_recovery_mock.html"
    job = jobs.create_job(
        "agent-error-waits",
        "Make a short edit",
        "instruction",
        [str(media)],
        {"url": fixture.resolve().as_uri(), "timeout_sec": 3, "chat_message": "请剪一个短视频"},
    )

    result = browser.run_cassette_browser_job(job)

    assert result["status"] == "needs_user"
    assert result["questions"][0]["reason"] == "completion_requires_hermes_review"
    assert all(error.get("code") != "cassette_agent_stalled" for error in result["errors"])


@pytest.mark.skipif(not browser.check_playwright(), reason="playwright is not installed")
def test_agent_request_failed_returns_immediately(cassette_env):
    browser.close_browser_sessions()
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    fixture = Path(__file__).parent / "fixtures" / "cassette_request_failed_mock.html"
    job = jobs.create_job(
        "agent-request-failed",
        "Make a short edit",
        "instruction",
        [str(media)],
        {"url": fixture.resolve().as_uri(), "timeout_sec": 30, "chat_message": "请剪一个短视频"},
    )

    result = browser.run_cassette_browser_job(job)

    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "cassette_request_failed"
    assert result["quality"]["stage_timings"]["agent"]["duration_sec"] < 30
    assert result["final_screenshot"]


@pytest.mark.skipif(not browser.check_playwright(), reason="playwright is not installed")
def test_agent_request_failed_new_chat_soft_retry(cassette_env):
    browser.close_browser_sessions()
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    fixture = Path(__file__).parent / "fixtures" / "cassette_request_failed_soft_retry_mock.html"
    job = jobs.create_job(
        "agent-request-failed-soft-retry",
        "Make a short edit",
        "instruction",
        [str(media)],
        {"url": fixture.resolve().as_uri(), "timeout_sec": 30, "chat_message": "请剪一个短视频"},
    )

    result = browser.run_cassette_browser_job(job)
    persisted = jobs.load_job(job["job_id"])

    assert result["status"] == "succeeded"
    assert result["outputs"][0]["download"] == "soft-retry-output.mp4"
    assert persisted["stage_timings"]["agent"]["attempts"] == 2
    assert any("new chat" in event.get("summary", "").lower() for event in persisted.get("progress_events") or [])


@pytest.mark.skipif(not browser.check_playwright(), reason="playwright is not installed")
def test_cancel_request_clicks_agent_stop_without_closing_browser(cassette_env):
    browser.close_browser_sessions()
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    fixture = Path(__file__).parent / "fixtures" / "cassette_stop_mock.html"
    job = jobs.create_job(
        "agent-stop",
        "Make a short edit",
        "instruction",
        [str(media)],
        {
            "url": fixture.resolve().as_uri(),
            "timeout_sec": 10,
            "chat_message": "请剪一个短视频",
            "cassette_session_id": "gateway-session-stop",
        },
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(browser.run_cassette_browser_job, job)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            persisted = jobs.load_job(job["job_id"])
            if persisted.get("current_stage") == "agent":
                break
            time.sleep(0.1)
        time.sleep(0.3)
        jobs.request_cancel(job["job_id"])
        result = future.result(timeout=10)

    persisted = jobs.load_job(job["job_id"])
    assert result["status"] == "cancelled"
    assert result["quality"]["progress_summary"].startswith("Cassette agent stop requested by /cut")
    assert any(
        event.get("operation_status") == "cancel_requested" and "Clicked stop control" in event.get("summary", "")
        for event in persisted.get("progress_events") or []
    )
    browser.close_browser_sessions("gateway-session-stop")


@pytest.mark.skipif(not browser.check_playwright(), reason="playwright is not installed")
def test_cancel_request_clicks_icon_only_composer_stop(cassette_env):
    browser.close_browser_sessions()
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    fixture = Path(__file__).parent / "fixtures" / "cassette_icon_stop_mock.html"
    job = jobs.create_job(
        "agent-icon-stop",
        "Make a short edit",
        "instruction",
        [str(media)],
        {
            "url": fixture.resolve().as_uri(),
            "timeout_sec": 10,
            "chat_message": "Please make a short edit",
            "cassette_session_id": "gateway-session-icon-stop",
        },
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(browser.run_cassette_browser_job, job)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            persisted = jobs.load_job(job["job_id"])
            if persisted.get("current_stage") == "agent":
                break
            time.sleep(0.1)
        time.sleep(0.3)
        jobs.request_cancel(job["job_id"])
        result = future.result(timeout=10)

    persisted = jobs.load_job(job["job_id"])
    assert result["status"] == "cancelled"
    assert any(
        event.get("operation_status") == "cancel_requested" and "composer icon stop control" in event.get("summary", "")
        for event in persisted.get("progress_events") or []
    )
    browser.close_browser_sessions("gateway-session-icon-stop")


@pytest.mark.skipif(not browser.check_playwright(), reason="playwright is not installed")
def test_agent_stage_sends_periodic_progress_snapshot(cassette_env, monkeypatch):
    monkeypatch.setenv("CASSETTE_PROGRESS_SNAPSHOT_SEC", "1")
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    fixture = Path(__file__).parent / "fixtures" / "cassette_agent_recovery_mock.html"
    calls = []

    def fake_notify(job, screenshot_path, summary):
        calls.append((job, screenshot_path, summary))
        return {"status": "sent", "platform": "qqbot", "message_id": "snapshot-ok"}

    monkeypatch.setattr(browser.notifier, "notify_progress_snapshot", fake_notify)
    job = jobs.create_job(
        "agent-snapshot",
        "wait for snapshot",
        "instruction",
        [str(media)],
        {
            "url": fixture.resolve().as_uri(),
            "timeout_sec": 10,
            "chat_message": "wait for snapshot",
            "delivery": {"platform": "qqbot", "chat_id": "qq_openid_raw", "chat_type": "dm"},
        },
    )

    result = browser.run_cassette_browser_job(job)

    assert result["status"] == "succeeded"
    assert calls
    assert Path(calls[0][1]).exists()
    assert calls[0][0]["current_stage"] == "agent"
    assert calls[0][0]["stage_timings"]["agent"]["duration_sec"] > 0
    persisted = jobs.load_job(job["job_id"])
    assert persisted["progress_snapshot_notifications"][0]["message_id"] == "snapshot-ok"


@pytest.mark.skipif(not browser.check_playwright(), reason="playwright is not installed")
def test_selects_cassette_model_before_submission(cassette_env):
    html = """
    <!doctype html>
    <html><body>
      <button title="DeepSeek V4 Flash · Low" id="trigger">model</button>
      <div role="dialog" id="dialog" style="display:none">
        <button>DeepSeek V4 Flash</button>
        <button>Kimi K2.6</button>
        <button>Low</button>
        <button>Medium</button>
        <button>High</button>
      </div>
      <script>
        window.selected = [];
        document.getElementById('trigger').addEventListener('click', () => {
          document.getElementById('dialog').style.display = 'block';
        });
        for (const button of document.querySelectorAll('#dialog button')) {
          button.addEventListener('click', () => window.selected.push(button.textContent));
        }
      </script>
    </body></html>
    """
    pw = browser._playwright()
    browser_instance = pw.chromium.launch(headless=True, args=["--no-sandbox"])
    try:
        page = browser_instance.new_page()
        page.set_content(html)

        result = browser._select_cassette_model(
            page,
            {"model_selection": {"model": "Kimi K2.6", "thinking_level": "Medium"}},
        )

        assert result["status"] == "selected"
        assert page.evaluate("window.selected") == ["Kimi K2.6", "Medium"]
    finally:
        browser_instance.close()


@pytest.mark.skipif(not browser.check_playwright(), reason="playwright is not installed")
def test_selects_cassette_model_with_chinese_ui(cassette_env):
    html = """
    <!doctype html>
    <html><body>
      <button title="DeepSeek V4 Flash · 低" id="trigger">model</button>
      <div role="dialog" id="dialog" style="display:none">
        <button>DeepSeek V4 Flash<br />高效 MoE 推理模型</button>
        <button>Kimi K2.6<br />长上下文 Moonshot 推理模型</button>
        <button title="轻量推理">低</button>
        <button title="平衡">中</button>
        <button title="深度推理">高</button>
      </div>
      <script>
        window.selected = [];
        document.getElementById('trigger').addEventListener('click', () => {
          document.getElementById('dialog').style.display = 'block';
        });
        for (const button of document.querySelectorAll('#dialog button')) {
          button.addEventListener('click', () => window.selected.push(button.textContent.trim().split('\\n')[0]));
        }
      </script>
    </body></html>
    """
    pw = browser._playwright()
    browser_instance = pw.chromium.launch(headless=True, args=["--no-sandbox"])
    try:
        page = browser_instance.new_page()
        page.set_content(html)

        result = browser._select_cassette_model(
            page,
            {"model_selection": {"model": "Kimi K2.6", "thinking_level": "Medium"}},
        )

        assert result["status"] == "selected"
        selected = page.evaluate("window.selected")
        assert selected[0].startswith("Kimi K2.6")
        assert selected[1] == "中"
    finally:
        browser_instance.close()


@pytest.mark.skipif(not browser.check_playwright(), reason="playwright is not installed")
def test_browser_mock_missing_asset_question_needs_user(cassette_env):
    fixture = Path(__file__).parent / "fixtures" / "cassette_question_mock.html"
    job = jobs.create_job(
        "sess",
        "Make a short edit",
        "instruction",
        [],
        {"url": fixture.resolve().as_uri(), "timeout_sec": 5},
    )
    result = browser.run_cassette_browser_job(job)
    assert result["status"] == "needs_user"
    assert result["questions"][0]["requires_user"] is True


@pytest.mark.skipif(not browser.check_playwright(), reason="playwright is not installed")
def test_browser_missing_media_statement_needs_user(cassette_env):
    fixture = Path(__file__).parent / "fixtures" / "cassette_missing_media_mock.html"
    job = jobs.create_job(
        "sess",
        "Make a short edit",
        "instruction",
        [],
        {"url": fixture.resolve().as_uri(), "timeout_sec": 5},
    )
    result = browser.run_cassette_browser_job(job)
    assert result["status"] == "needs_user"
    assert result["questions"][0]["reason"] == "missing_required_asset"


@pytest.mark.skipif(not browser.check_playwright(), reason="playwright is not installed")
def test_browser_mock_chat_input_failure(cassette_env):
    browser.close_browser_sessions()
    fixture = Path(__file__).parent / "fixtures" / "cassette_no_chat_mock.html"
    job = jobs.create_job(
        "sess",
        "Make a short edit",
        "instruction",
        [],
        {"url": fixture.resolve().as_uri(), "timeout_sec": 5, "cassette_session_id": "chat-input-fail-session"},
    )
    result = browser.run_cassette_browser_job(job)
    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "chat_input_failed"
    assert "chat-input-fail-session" not in browser._BROWSER_SESSIONS


@pytest.mark.skipif(not browser.check_playwright(), reason="playwright is not installed")
def test_browser_network_failure_returns_connectivity_error(cassette_env):
    job = jobs.create_job(
        "sess",
        "Make a short edit",
        "instruction",
        [],
        {"url": "http://127.0.0.1:9/not-running", "timeout_sec": 3},
    )
    result = browser.run_cassette_browser_job(job)
    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "cassette_unreachable"


@pytest.mark.skipif(not browser.check_playwright(), reason="playwright is not installed")
def test_browser_upload_does_not_click_visible_file_picker(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    fixture = Path(__file__).parent / "fixtures" / "cassette_upload_button_only.html"
    job = jobs.create_job(
        "sess",
        "Make a short edit",
        "instruction",
        [str(media)],
        {"url": fixture.resolve().as_uri(), "timeout_sec": 3},
    )
    result = browser.run_cassette_browser_job(job)
    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "asset_upload_failed"
    assert "programmatic file input" in result["errors"][0]["message"]


@pytest.mark.skipif(not browser.check_playwright(), reason="playwright is not installed")
def test_run_job_wait_false_background_worker(cassette_env):
    fixture = Path(__file__).parent / "fixtures" / "cassette_mock.html"
    payload = tools.cassette_run_job(
        {
            "prompt": "Make a short edit",
            "session_id": "background",
            "url": fixture.resolve().as_uri(),
            "wait": False,
            "timeout_sec": 10,
        }
    )
    import json

    data = json.loads(payload)
    assert data["ok"] is True
    job_id = data["job_id"]
    deadline = time.time() + 10
    status = None
    while time.time() < deadline:
        status_payload = json.loads(tools.cassette_job_status({"job_id": job_id}))
        status = status_payload["data"]["job"]["status"]
        if status in {"succeeded", "failed", "needs_user", "timed_out", "cancelled"}:
            break
        time.sleep(0.2)
    assert status == "succeeded"
