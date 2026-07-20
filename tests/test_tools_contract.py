from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from pathlib import Path
from types import SimpleNamespace

from cassette import jobs, tools


HANDLERS = [
    tools.cassette_ingest_media,
    tools.cassette_list_assets,
    tools.cassette_make_prompt,
    tools.cassette_match_bgm,
    tools.cassette_match_exact_bgm,
    tools.jamendo_music_matcher,
    tools.cassette_answer_question,
    tools.cassette_run_job,
    tools.cassette_job_status,
    tools.cassette_review_completion,
    tools.cassette_cancel_job,
]


def _assert_semantic_edit_gate(
    result,
    instruction: str,
    *,
    asset_count: int = 1,
    language: str = "zh",
    expect_prompt_optimization: bool = True,
) -> None:
    assert result is not None
    assert result["action"] == "rewrite"
    assert result["text"].startswith(instruction)
    assert f"Cassette gateway assets available: {asset_count} asset(s)" in result["text"]
    assert "Hermes must semantically decide" in result["text"]
    assert "Do not rely on keyword matching" in result["text"]
    if expect_prompt_optimization:
        if language == "en":
            assert "Would you like me to optimize your edit instruction" in result["text"]
        else:
            assert "是否需要我先把你的剪辑指令优化" in result["text"]


def _cassette_debug_events(asset_root: Path) -> list[dict]:
    log_path = asset_root / "logs" / "cassette.log"
    if not log_path.exists():
        return []
    events = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("event"):
            events.append(payload)
    return events


def test_all_handlers_return_json_string(cassette_env):
    for handler in HANDLERS:
        result = handler({})
        assert isinstance(result, str)
        payload = json.loads(result)
        assert "ok" in payload


def test_ingest_handler_success(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    payload = json.loads(tools.cassette_ingest_media({"source_path": str(media), "chat_id": "chat"}))
    assert payload["ok"] is True
    assert payload["data"]["asset_id"].startswith("asset_")
    assert "saved_path" not in payload["data"]
    assert "manifest_path" not in payload["data"]


def test_list_assets_scrubs_local_paths_and_raw_delivery(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    tools.cassette_ingest_media(
        {
            "source_path": str(media),
            "chat_id": "raw_chat_id",
            "user_id": "raw_user_id",
            "platform": "qqbot",
            "session_id": "scrub",
        }
    )

    payload = json.loads(tools.cassette_list_assets({"session_id": "scrub"}))
    serialized = json.dumps(payload, ensure_ascii=False)

    assert payload["ok"] is True
    assert "saved_path" not in serialized
    assert "manifest_path" not in serialized
    assert str(media) not in serialized
    assert "raw_chat_id" not in serialized
    assert "raw_user_id" not in serialized
    assert payload["data"]["manifest"]["assets"][0]["stored"] is True
    assert payload["data"]["manifest"]["delivery"]["has_raw_target"] is True


def test_make_prompt_missing_assets_error(cassette_env):
    payload = json.loads(tools.cassette_make_prompt({"instruction": "edit this", "session_id": "empty"}))
    assert payload["ok"] is False
    assert payload["error"]["code"] == "missing_critical_assets"


def test_ingest_gateway_media_saves_authorized_video_without_running_job(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    sent = []
    source = SimpleNamespace(
        platform=SimpleNamespace(value="weixin"),
        chat_id="wxid_chat_raw",
        user_id="wxid_user_raw",
    )
    event = SimpleNamespace(
        source=source,
        media_urls=[str(media)],
        media_types=["video/mp4"],
        text="",
        message_id="raw_message_id",
    )
    gateway = SimpleNamespace(
        _is_user_authorized=lambda _: True,
        adapters={"weixin": SimpleNamespace(send=lambda chat_id, text: sent.append((chat_id, text)))},
    )

    result = tools.ingest_gateway_media(event=event, gateway=gateway)

    assert result is not None
    assert result["action"] == "skip"
    assert result["reason"] == "cassette_media_saved_waiting_for_instruction"
    assert result["asset_count"] == 1
    assert result["total_asset_count"] == 1
    assert result["reply_sent"] is True
    assert sent == [("wxid_chat_raw", "已保存素材 1 个。请继续发送素材，或发送剪辑指令后我会交给 Cassette 处理。")]
    assert "wxid_chat_raw" not in json.dumps(result, ensure_ascii=False)
    manifests = list(Path(cassette_env["asset_root"]).glob("sessions/*/manifest.json"))
    assert len(manifests) == 1
    manifest_data = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest_data["delivery"]["platform"] == "weixin"
    assert manifest_data["delivery"]["chat_id"] == "wxid_chat_raw"
    assert manifest_data["delivery"]["user_id"] == "wxid_user_raw"
    assert manifest_data["delivery"]["message_id"] == "raw_message_id"


def test_ingest_gateway_media_binds_followup_instruction_to_saved_assets(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    source = SimpleNamespace(
        platform=SimpleNamespace(value="weixin"),
        chat_id="wxid_chat_raw",
        user_id="wxid_user_raw",
    )
    gateway = SimpleNamespace(_is_user_authorized=lambda _: True)
    media_event = SimpleNamespace(
        source=source,
        media_urls=[str(media)],
        media_types=["video/mp4"],
        text="",
        message_id="raw_message_id",
    )
    tools.ingest_gateway_media(event=media_event, gateway=gateway)
    instruction_event = SimpleNamespace(
        source=source,
        media_urls=[],
        media_types=[],
        text="剪成 10 秒短视频，加中文字幕",
        message_id="raw_text_message_id",
    )

    result = tools.ingest_gateway_media(event=instruction_event, gateway=gateway)

    _assert_semantic_edit_gate(result, "剪成 10 秒短视频，加中文字幕")
    assert "Do not call cassette_list_assets" in result["text"]
    assert "cassette_run_job with cassette_make_prompt" not in result["text"]
    assert "wxid_chat_raw" not in result["text"]


def test_ingest_gateway_media_requests_prompt_optimization_choice_with_fixed_reply(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    sent = []
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"),
        chat_id="qq_openid_raw",
        user_id="qq_user_raw",
        chat_type="dm",
    )
    gateway = SimpleNamespace(
        _is_user_authorized=lambda _: True,
        adapters={"qqbot": SimpleNamespace(send=lambda chat_id, text: sent.append((chat_id, text)))},
    )
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[str(media)],
            media_types=["video/mp4"],
            text="",
            message_id="raw_message_id",
        ),
        gateway=gateway,
    )

    result = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="/edit 剪成 10 秒短视频，加中文字幕",
            message_id="raw_text_message_id",
        ),
        gateway=gateway,
    )

    assert result is not None
    assert result["action"] == "skip"
    assert result["reason"] == "cassette_prompt_optimization_choice_requested"
    assert result["reply_sent"] is True
    assert "是否需要我先把你的剪辑指令优化" in sent[-1][1]
    assert "qq_openid_raw" not in json.dumps(result, ensure_ascii=False)


def test_ingest_gateway_media_pings_cassette_before_prompt_choice(cassette_env, monkeypatch):
    monkeypatch.setenv("CASSETTE_PING_ON_GATEWAY_INSTRUCTION", "1")
    monkeypatch.setattr(
        tools.browser,
        "check_cassette_connectivity",
        lambda: {"ok": False, "code": "cassette_unreachable"},
    )
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    sent = []
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"),
        chat_id="qq_openid_raw",
        user_id="qq_user_raw",
        chat_type="dm",
    )
    gateway = SimpleNamespace(
        _is_user_authorized=lambda _: True,
        adapters={"qqbot": SimpleNamespace(send=lambda chat_id, text: sent.append((chat_id, text)))},
    )
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[str(media)],
            media_types=["video/mp4"],
            text="",
            message_id="raw_message_id",
        ),
        gateway=gateway,
    )

    result = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="/edit 剪成 10 秒短视频，加中文字幕",
            message_id="raw_text_message_id",
        ),
        gateway=gateway,
    )

    assert result is not None
    assert result["action"] == "skip"
    assert result["reason"] == "cassette_unreachable"
    assert sent[-1][1] == "无法连接 Cassette，请检查网络设置。"
    assert "qq_openid_raw" not in json.dumps(result, ensure_ascii=False)


def test_ingest_gateway_media_reports_saved_asset_status_without_llm(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    sent = []
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"),
        chat_id="qq_openid_raw",
        user_id="qq_user_raw",
        chat_type="dm",
    )
    gateway = SimpleNamespace(
        _is_user_authorized=lambda _: True,
        adapters={"qqbot": SimpleNamespace(send=lambda chat_id, text: sent.append((chat_id, text)))},
    )
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[str(media)],
            media_types=["video/mp4"],
            text="",
            message_id="raw_message_id",
        ),
        gateway=gateway,
    )

    result = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="/check_assets",
            message_id="status_message_id",
        ),
        gateway=gateway,
    )

    assert result is not None
    assert result["action"] == "skip"
    assert result["reason"] == "cassette_asset_status_reported"
    assert result["asset_count"] == 1
    assert result["reply_sent"] is True
    assert "当前 Cassette 会话已保存素材 1 个" in sent[-1][1]
    assert "视频 1 个" in sent[-1][1]
    assert "qq_openid_raw" not in sent[-1][1]


def test_ingest_gateway_media_does_not_treat_receive_complaint_as_asset_check(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    sent = []
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"),
        chat_id="qq_openid_raw",
        user_id="qq_user_raw",
        chat_type="dm",
    )
    gateway = SimpleNamespace(
        _is_user_authorized=lambda _: True,
        adapters={"qqbot": SimpleNamespace(send=lambda chat_id, text: sent.append((chat_id, text)))},
    )
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[str(media)],
            media_types=["video/mp4"],
            text="",
            message_id="raw_message_id",
        ),
        gateway=gateway,
    )

    result = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="I didn't receive the video",
            message_id="complaint_message_id",
        ),
        gateway=gateway,
    )

    assert result is None
    assert len(sent) == 1


def test_ingest_gateway_media_loads_prompt_optimizer_document(cassette_env, monkeypatch, tmp_path):
    doc = tmp_path / "optimizer.md"
    doc.write_text(
        "CUSTOM OPTIMIZER DOC\n\nPreserve explicit user intent. Ask for 确认 before Cassette.",
        encoding="utf-8",
    )
    monkeypatch.setenv("CASSETTE_PROMPT_OPTIMIZER_DOC", str(doc))
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    source = SimpleNamespace(
        platform=SimpleNamespace(value="weixin"),
        chat_id="wxid_chat_raw",
        user_id="wxid_user_raw",
    )
    gateway = SimpleNamespace(_is_user_authorized=lambda _: True)
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[str(media)],
            media_types=["video/mp4"],
            text="",
            message_id="raw_message_id",
        ),
        gateway=gateway,
    )

    result = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="剪成 10 秒短视频，加中文字幕",
            message_id="raw_text_message_id",
        ),
        gateway=gateway,
    )

    _assert_semantic_edit_gate(result, "剪成 10 秒短视频，加中文字幕")
    assert "CUSTOM OPTIMIZER DOC" not in result["text"]

    accepted = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="优化",
            message_id="optimize_choice_message_id",
        ),
        gateway=gateway,
    )

    assert accepted is not None
    assert accepted["action"] == "rewrite"
    assert "smart BGM choice is required" in accepted["text"]
    assert "CUSTOM OPTIMIZER DOC" not in accepted["text"]

    bgm_declined = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="不需要 BGM",
            message_id="bgm_choice_message_id",
        ),
        gateway=gateway,
    )

    assert bgm_declined is not None
    assert bgm_declined["action"] == "rewrite"
    assert "Cassette prompt optimization accepted" in bgm_declined["text"]
    assert "CUSTOM OPTIMIZER DOC" in bgm_declined["text"]
    assert "Preserve explicit user intent" in bgm_declined["text"]
    assert "professional editing brief optimizer" not in bgm_declined["text"]


def test_ingest_gateway_media_declines_prompt_optimization_and_uses_original_instruction(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    source = SimpleNamespace(
        platform=SimpleNamespace(value="weixin"),
        chat_id="wxid_chat_raw",
        user_id="wxid_user_raw",
    )
    gateway = SimpleNamespace(_is_user_authorized=lambda _: True)
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[str(media)],
            media_types=["video/mp4"],
            text="",
            message_id="raw_message_id",
        ),
        gateway=gateway,
    )
    original = "剪成 10 秒短视频，加中文字幕"
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text=original,
            message_id="raw_text_message_id",
        ),
        gateway=gateway,
    )

    declined = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="不优化，直接开始",
            message_id="decline_message_id",
        ),
        gateway=gateway,
    )

    assert declined is not None
    assert declined["action"] == "rewrite"
    assert "smart BGM choice is required" in declined["text"]

    bgm_declined = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="不需要 BGM",
            message_id="bgm_choice_message_id",
        ),
        gateway=gateway,
    )

    assert bgm_declined is not None
    assert bgm_declined["action"] == "rewrite"
    assert bgm_declined["text"].startswith(original)
    assert "prompt optimization declined" in bgm_declined["text"]
    assert "Use the original edit instruction above exactly" in bgm_declined["text"]
    assert "cassette_list_assets" in bgm_declined["text"]
    assert "cassette_make_prompt" in bgm_declined["text"]
    assert "cassette_run_job" in bgm_declined["text"]
    assert "prompt=cassette_make_prompt.data.prompt" in bgm_declined["text"]
    assert "chat_message=cassette_make_prompt.data.chat_message" in bgm_declined["text"]
    assert "progress screenshot notifications" in bgm_declined["text"]
    assert "gateway delivery behavior" in bgm_declined["text"]
    assert "not a lightweight/direct browser path" in bgm_declined["text"]
    assert "professional editing brief optimizer" not in bgm_declined["text"]
    assert "wxid_chat_raw" not in bgm_declined["text"]


def test_smart_bgm_accept_requests_exact_song_recommendation_menu(cassette_env, monkeypatch):
    monkeypatch.setattr(
        tools,
        "_freetouse_category_summary",
        lambda: "- Action (Video); related tags: adrenaline, sports\n- Chill (Genre); related tags: mellow, relaxed",
    )
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    source = SimpleNamespace(
        platform=SimpleNamespace(value="weixin"),
        chat_id="wxid_chat_raw",
        user_id="wxid_user_raw",
    )
    sent = []
    gateway = SimpleNamespace(
        _is_user_authorized=lambda _: True,
        adapters={"weixin": SimpleNamespace(send=lambda chat_id, text: sent.append((chat_id, text)))},
    )
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[str(media)],
            media_types=["video/mp4"],
            text="",
            message_id="raw_message_id",
        ),
        gateway=gateway,
    )
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="剪成燃一点的运动短视频",
            message_id="raw_text_message_id",
        ),
        gateway=gateway,
    )
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="不优化",
            message_id="decline_message_id",
        ),
        gateway=gateway,
    )

    result = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="需要 BGM",
            message_id="bgm_choice_message_id",
        ),
        gateway=gateway,
    )

    assert result is not None
    assert result["action"] == "rewrite"
    assert "exactly five numbered options" in result["text"]
    assert "recommend exactly 3 real songs" in result["text"]
    assert "1.《歌名》- 歌手" in result["text"]
    assert "4. 换一批" in result["text"]
    assert "5. 随机匹配" in result["text"]
    assert "第 4 和第 5 项是必须保留的控制选项" in result["text"]
    assert "Do not call any tool yet" in result["text"]
    assert "cassette_match_exact_bgm" in result["text"]
    assert "wxid_chat_raw" not in result["text"]


def test_smart_bgm_recommendation_menu_requires_five_english_options(cassette_env):
    result = tools._request_exact_bgm_recommendations(
        "gateway_media_telegram_chat_session",
        "add a cute popular song to this cat video",
        1,
        optimization_enabled=False,
        language="en",
    )

    assert result["action"] == "rewrite"
    assert "exactly five numbered options" in result["text"]
    assert '1. "Song Title" - Artist' in result["text"]
    assert "4. Another batch" in result["text"]
    assert "5. Random match" in result["text"]
    assert "Options 4 and 5 are mandatory control options" in result["text"]


def test_exact_bgm_selection_choice_calls_exact_tool(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    source = SimpleNamespace(
        platform=SimpleNamespace(value="weixin"),
        chat_id="wxid_chat_raw",
        user_id="wxid_user_raw",
    )
    gateway = SimpleNamespace(_is_user_authorized=lambda _: True)
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source, media_urls=[str(media)], media_types=["video/mp4"], text="", message_id="raw_message_id"
        ),
        gateway=gateway,
    )
    tools._mark_initial_edit_choices_completed(
        tools._gateway_session_id(SimpleNamespace(source=source), None),
        optimization_enabled=False,
        smart_bgm_enabled=True,
        source="test",
    )
    session_id = tools._gateway_session_id(SimpleNamespace(source=source), None)
    tools._save_pending_edit(
        session_id,
        "剪成燃一点的运动短视频",
        1,
        "awaiting_exact_bgm_selection",
        optimization_enabled=False,
        continue_after_match=True,
    )

    result = tools.ingest_gateway_media(
        event=SimpleNamespace(source=source, media_urls=[], media_types=[], text="2", message_id="choice_message_id"),
        gateway=gateway,
    )

    assert result is not None
    assert result["action"] == "rewrite"
    assert "selected smart BGM recommendation #2" in result["text"]
    assert "cassette_match_exact_bgm" in result["text"]
    assert "Read ONLY the immediately previous assistant recommendation menu" in result["text"]
    assert "exact numbered line `2.`" in result["text"]
    assert "cannot unambiguously extract both title and artist" in result["text"]
    assert "jamendo_music_matcher" in result["text"] or "cassette_match_bgm" in result["text"]
    assert tools._load_pending_edit(session_id) is None


def test_cassette_match_exact_bgm_sanitizes_numbered_menu_line(cassette_env, monkeypatch):
    observed = {}

    def fake_match_exact_bgm(**kwargs):
        observed.update(kwargs)
        return {
            "status": "matched",
            "provider": "musicsquare_exact",
            "artist": "房东的猫",
            "title": "New Boy",
            "query": "New Boy 房东的猫",
            "source": "qq",
            "track_id": "mid-1",
            "candidateCount": 1,
            "eligibleCandidates": [],
            "attempts": [],
        }

    monkeypatch.setattr(tools.exact_bgm, "match_exact_bgm", fake_match_exact_bgm)

    payload = json.loads(
        tools.cassette_match_exact_bgm(
            {
                "session_id": "exact-menu-line",
                "instruction": "剪成产品促销广告",
                "title": "2. 《New Boy》 - 房东的猫：轻快阳光的节奏，能传递产品的青春活力感",
                "download": False,
            }
        )
    )

    assert payload["ok"] is True
    assert observed["title"] == "New Boy"
    assert observed["artist"] == "房东的猫"


def test_cassette_match_exact_bgm_logs_search_failure_details(cassette_env, monkeypatch):
    def fake_match_exact_bgm(**kwargs):
        del kwargs
        raise tools.CassetteError(
            "exact_bgm_no_search_results",
            "Exact BGM search returned no eligible song for the requested title/artist",
            {
                "attempts": [
                    {
                        "mode": "title_artist",
                        "query": "Chef Song Test Artist",
                        "candidate_count": 3,
                        "eligible_count": 0,
                        "strict_title": True,
                    },
                    {
                        "mode": "title_only",
                        "query": "Chef Song",
                        "candidate_count": 1,
                        "eligible_count": 0,
                        "downloadable_count": 0,
                        "strict_title": True,
                        "candidate_failures": [
                            {
                                "source": "netease",
                                "track_id": "123",
                                "title": "Chef Song",
                                "artist": "Test Artist",
                                "code": "exact_bgm_invalid_audio_url",
                                "details": {"message": "unknown url type: 'None'"},
                                "audio_url": {"status": "valid", "host": "api.example.test"},
                            }
                        ],
                    },
                ],
            },
        )

    monkeypatch.setattr(tools.exact_bgm, "match_exact_bgm", fake_match_exact_bgm)

    payload = json.loads(
        tools.cassette_match_exact_bgm(
            {
                "session_id": "exact-fail-log",
                "instruction": "剪一个美食视频",
                "title": "Chef Song",
                "artist": "Test Artist",
                "download": True,
            }
        )
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "exact_bgm_no_search_results"
    events = _cassette_debug_events(cassette_env["asset_root"])
    failed = [event for event in events if event.get("event") == "bgm_exact_search_failed"][-1]
    assert failed["session_hash"] == tools.manifest.resolve_session_hash(session_id="exact-fail-log")
    assert failed["code"] == "exact_bgm_no_search_results"
    assert failed["title"] == "Chef Song"
    assert failed["artist"] == "Test Artist"
    assert failed["attempts"][0]["query"] == "Chef Song Test Artist"
    assert failed["attempts"][0]["candidate_count"] == 3
    assert failed["attempts"][1]["downloadable_count"] == 0
    assert failed["attempts"][1]["candidate_failures"][0]["code"] == "exact_bgm_invalid_audio_url"
    assert failed["attempts"][1]["candidate_failures"][0]["details"]["message"] == "unknown url type: 'None'"


def test_exact_bgm_selection_can_request_new_batch(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"), chat_id="qq_openid_raw", user_id="qq_user_raw", chat_type="dm"
    )
    gateway = SimpleNamespace(_is_user_authorized=lambda _: True)
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source, media_urls=[str(media)], media_types=["video/mp4"], text="", message_id="raw_message_id"
        ),
        gateway=gateway,
    )
    session_id = tools._gateway_session_id(SimpleNamespace(source=source), None)
    tools._save_pending_edit(
        session_id,
        "做成安静未来感菜单背景",
        1,
        "awaiting_exact_bgm_selection",
        optimization_enabled=False,
        continue_after_match=True,
        recommendation_round=1,
    )

    result = tools.ingest_gateway_media(
        event=SimpleNamespace(source=source, media_urls=[], media_types=[], text="4", message_id="change_batch"),
        gateway=gateway,
    )

    assert result is not None
    assert result["action"] == "rewrite"
    assert "recommend exactly 3 real songs" in result["text"]
    assert "This is recommendation batch 2" in result["text"]
    assert tools._load_pending_edit(session_id)["recommendation_round"] == 2


def test_exact_bgm_selection_random_uses_plugin_selected_provider(cassette_env, monkeypatch):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"), chat_id="qq_openid_raw", user_id="qq_user_raw", chat_type="dm"
    )
    gateway = SimpleNamespace(_is_user_authorized=lambda _: True)
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source, media_urls=[str(media)], media_types=["video/mp4"], text="", message_id="raw_message_id"
        ),
        gateway=gateway,
    )
    session_id = tools._gateway_session_id(SimpleNamespace(source=source), None)
    tools._save_pending_edit(
        session_id,
        "做成安静未来感菜单背景",
        1,
        "awaiting_exact_bgm_selection",
        optimization_enabled=False,
        continue_after_match=True,
    )
    monkeypatch.setattr(tools.random, "choice", lambda items: "exact_song")

    result = tools.ingest_gateway_media(
        event=SimpleNamespace(source=source, media_urls=[], media_types=[], text="5", message_id="random_choice"),
        gateway=gateway,
    )

    assert result is not None
    assert result["action"] == "rewrite"
    assert "selected `exact_song` as the primary provider" in result["text"]
    assert "cassette_match_exact_bgm" in result["text"]
    assert tools._load_pending_edit(session_id) is None


def test_exact_bgm_selection_text_supplements_requirements_and_recommends_new_batch(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"), chat_id="qq_openid_raw", user_id="qq_user_raw", chat_type="dm"
    )
    gateway = SimpleNamespace(_is_user_authorized=lambda _: True)
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source, media_urls=[str(media)], media_types=["video/mp4"], text="", message_id="raw_message_id"
        ),
        gateway=gateway,
    )
    session_id = tools._gateway_session_id(SimpleNamespace(source=source), None)
    tools._save_pending_edit(
        session_id,
        "做成暗黑反差剪辑",
        1,
        "awaiting_exact_bgm_selection",
        optimization_enabled=False,
        continue_after_match=True,
        recommendation_round=1,
    )

    result = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source, media_urls=[], media_types=[], text="想要更复古一点的灵魂乐男声", message_id="bgm_supplement"
        ),
        gateway=gateway,
    )

    assert result is not None
    assert result["action"] == "rewrite"
    assert "用户补充的 BGM 需求：想要更复古一点的灵魂乐男声" in result["text"]
    assert "This is recommendation batch 2" in result["text"]
    pending = tools._load_pending_edit(session_id)
    assert pending is not None
    assert "想要更复古一点的灵魂乐男声" in pending["instruction"]
    assert pending["recommendation_round"] == 2


def test_cassette_match_bgm_retries_empty_search_and_registers_audio_asset(cassette_env, monkeypatch):
    calls = []
    notifications = []

    def fake_search(query, limit=10, order="random"):
        calls.append((query, limit, order))
        if query != "action adrenaline sports":
            return []
        tracks = []
        for index in range(6):
            tracks.append(
                {
                    "id": f"{order}-{index}",
                    "title": f"Fast Motion {index}",
                    "is_premium": False,
                    "artists": [[0, {"name": "Test Artist"}]],
                }
            )
        return tracks

    def fake_download(session_id, track, query):
        sess_hash = tools.manifest.resolve_session_hash(session_id=session_id)
        media_dir = tools.manifest.get_session_dir(sess_hash) / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        audio = media_dir / "test-artist-fast-motion.mp3"
        audio.write_bytes(b"fake mp3 bytes")
        data = tools.manifest.ingest_internal_asset(
            str(audio),
            session_id=session_id,
            original_name="Test Artist - Fast Motion.mp3",
            media_type="audio",
            caption=f"Smart BGM matched from Free To Use. Search query: {query}.",
            metadata={"source": "freetouse", "track_id": track["id"], "query": query},
        )
        return data | {
            "status": "downloaded",
            "track_id": track["id"],
            "artist": "Test Artist",
            "title": "Fast Motion",
            "query": query,
            "source_rank": track.get("_cassette_source_rank") or "",
        }

    monkeypatch.setattr(tools, "_freetouse_search_tracks", fake_search)
    monkeypatch.setattr(tools.random, "choice", lambda items: items[-1])
    monkeypatch.setattr(tools, "_download_freetouse_track", fake_download)
    monkeypatch.setattr(
        tools.notifier,
        "notify_gateway_text",
        lambda delivery, message, reason="text": (
            notifications.append((delivery, message, reason)) or {"status": "sent"}
        ),
    )
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    source = SimpleNamespace(
        platform=SimpleNamespace(value="weixin"),
        chat_id="wxid_chat_raw",
        user_id="wxid_user_raw",
    )
    gateway = SimpleNamespace(_is_user_authorized=lambda _: True)
    saved = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[str(media)],
            media_types=["video/mp4"],
            text="",
            message_id="raw_message_id",
        ),
        gateway=gateway,
    )

    payload = json.loads(
        tools.cassette_match_bgm(
            {
                "session_id": saved["session_id"],
                "instruction": "剪成燃一点的运动短视频",
                "search_queries": ["cinematic empty", "energetic empty", "Action Adrenaline Sports"],
                "optimization_enabled": False,
            }
        )
    )

    assert payload["ok"] is True
    data = payload["data"]
    assert data["status"] == "downloaded"
    assert data["selected"]["query"] == "action adrenaline sports"
    assert data["selected"]["source_rank"] == "popular"
    assert "请添加已上传的智能匹配 BGM「Test Artist - Fast Motion」作为背景音乐" in data["effective_instruction"]
    assert (
        data["user_message"]
        == "已智能匹配 BGM：Test Artist - Fast Motion。搜索关键词：action adrenaline sports。我会继续后续剪辑流程。"
    )
    assert notifications[-1][1] == data["user_message"]
    assert notifications[-1][2] == "smart_bgm"
    assert calls == [
        ("cinematic empty", 5, "staff_order"),
        ("cinematic empty", 5, "downloads"),
        ("energetic empty", 5, "staff_order"),
        ("energetic empty", 5, "downloads"),
        ("action adrenaline sports", 5, "staff_order"),
        ("action adrenaline sports", 5, "downloads"),
    ]
    manifests = list(Path(cassette_env["asset_root"]).glob("sessions/*/manifest.json"))
    manifest_data = max(manifests, key=lambda path: path.stat().st_mtime).read_text(encoding="utf-8")
    assets = json.loads(manifest_data)["assets"]
    assert len(assets) == 2
    assert any(
        asset.get("media_type") == "audio" and asset.get("metadata", {}).get("source") == "freetouse"
        for asset in assets
    )
    audio_asset = next(asset for asset in assets if asset.get("media_type") == "audio")
    assert Path(audio_asset["saved_path"]).parent.name == "media"
    events = _cassette_debug_events(cassette_env["asset_root"])
    done = [event for event in events if event.get("event") == "bgm_freetouse_search_done"][-1]
    assert done["attempted_queries"] == ["cinematic empty", "energetic empty", "action adrenaline sports"]
    assert done["zero_result_queries"] == ["cinematic empty", "energetic empty"]
    assert done["selected"]["query"] == "action adrenaline sports"


def test_smart_bgm_network_failure_does_not_block_direct_flow(cassette_env, monkeypatch):
    def fake_search(query, limit=10, order="random"):
        del query, limit, order
        raise RuntimeError("api unavailable")

    monkeypatch.setattr(tools, "_freetouse_search_tracks", fake_search)
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    source = SimpleNamespace(
        platform=SimpleNamespace(value="weixin"),
        chat_id="wxid_chat_raw",
        user_id="wxid_user_raw",
    )
    gateway = SimpleNamespace(_is_user_authorized=lambda _: True)
    saved = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[str(media)],
            media_types=["video/mp4"],
            text="",
            message_id="raw_message_id",
        ),
        gateway=gateway,
    )
    payload = json.loads(
        tools.cassette_match_bgm(
            {
                "session_id": saved["session_id"],
                "instruction": "剪成 10 秒短视频，加中文字幕",
                "search_queries": ["cinematic upbeat", "advertising positive", "chill mellow"],
                "optimization_enabled": False,
            }
        )
    )

    assert payload["ok"] is True
    assert payload["data"]["status"] == "skipped"
    assert payload["data"]["code"] == "bgm_match_failed"
    assert payload["data"]["effective_instruction"] == "剪成 10 秒短视频，加中文字幕"
    assert "Use effective_instruction directly" in payload["data"]["hermes_next_step"]
    assert "智能 BGM 匹配未成功（bgm_match_failed）" in payload["data"]["user_message"]


def test_cassette_match_bgm_uses_telegram_default_language_from_manifest(cassette_env, monkeypatch):
    notifications = []

    monkeypatch.setattr(
        tools,
        "_match_and_download_smart_bgm",
        lambda session_id, instruction, search_queries: {
            "status": "skipped",
            "code": "bgm_match_failed",
            "queries": search_queries,
        },
    )
    monkeypatch.setattr(
        tools.notifier,
        "notify_gateway_text",
        lambda delivery, message, reason="text": (
            notifications.append((delivery, message, reason)) or {"status": "sent"}
        ),
    )
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    source = SimpleNamespace(
        platform=SimpleNamespace(value="telegram"),
        chat_id="telegram_chat_raw",
        user_id="telegram_user_raw",
    )
    gateway = SimpleNamespace(_is_user_authorized=lambda _: True)
    saved = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[str(media)],
            media_types=["video/mp4"],
            text="",
            message_id="telegram_message_raw",
        ),
        gateway=gateway,
    )

    payload = json.loads(
        tools.cassette_match_bgm(
            {
                "session_id": saved["session_id"],
                "instruction": "Make a short travel reel",
                "search_queries": ["ambient travel"],
                "optimization_enabled": False,
            }
        )
    )

    assert payload["ok"] is True
    data = payload["data"]
    assert data["status"] == "skipped"
    assert data["user_message"].startswith("Smart BGM matching did not succeed")
    assert "cassette_language='en'" in data["hermes_next_step"]
    assert notifications[-1][1] == data["user_message"]
    assert notifications[-1][0]["platform"] == "telegram"
    assert "telegram_chat_raw" not in json.dumps(data, ensure_ascii=False)


def test_cassette_match_bgm_reports_exact_song_fallback(cassette_env, monkeypatch):
    monkeypatch.setattr(
        tools,
        "_match_and_download_smart_bgm",
        lambda session_id, instruction, search_queries: {
            "status": "downloaded",
            "asset_id": "asset_audio",
            "track_id": "track_1",
            "artist": "Fallback Artist",
            "title": "Fallback Track",
            "query": search_queries[0],
            "source_rank": "staff_picks",
        },
    )
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"),
        chat_id="qq_openid_raw",
        user_id="qq_user_raw",
    )
    gateway = SimpleNamespace(_is_user_authorized=lambda _: True)
    saved = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[str(media)],
            media_types=["video/mp4"],
            text="",
            message_id="raw_message_id",
        ),
        gateway=gateway,
    )

    payload = json.loads(
        tools.cassette_match_bgm(
            {
                "session_id": saved["session_id"],
                "instruction": "剪成 10 秒短视频",
                "search_queries": ["cinematic cooking"],
                "optimization_enabled": False,
                "fallback_from": "exact_bgm",
                "fallback_reason": "exact_bgm_no_search_results",
            }
        )
    )

    assert payload["ok"] is True
    data = payload["data"]
    assert data["fallback"] == {"from": "exact_bgm", "reason": "exact_bgm_no_search_results"}
    assert "精确歌曲匹配未成功，已切换到备用智能 BGM 匹配" in data["user_message"]
    assert "已智能匹配 BGM：Fallback Artist - Fallback Track" in data["user_message"]
    events = _cassette_debug_events(cassette_env["asset_root"])
    done = [event for event in events if event.get("event") == "bgm_freetouse_search_done"][-1]
    assert done["fallback_from"] == "exact_bgm"
    assert done["fallback_reason"] == "exact_bgm_no_search_results"
    assert done["search_queries"] == ["cinematic cooking"]
    assert done["selected"]["title"] == "Fallback Track"
    assert done["selected"]["track_id"] == "track_1"


def test_cassette_match_bgm_can_only_register_material(cassette_env, monkeypatch):
    monkeypatch.setattr(
        tools,
        "_match_and_download_smart_bgm",
        lambda session_id, instruction, search_queries: {
            "status": "downloaded",
            "asset_id": "asset_audio",
            "track_id": "track_1",
            "artist": "Test Artist",
            "title": "Travel Light",
            "query": search_queries[0],
            "source_rank": "staff_picks",
        },
    )

    payload = json.loads(
        tools.cassette_match_bgm(
            {
                "session_id": "music-only-session",
                "instruction": "旅行感、轻快、适合航拍",
                "search_queries": ["travel sunny"],
                "optimization_enabled": False,
                "continue_after_match": False,
            }
        )
    )

    assert payload["ok"] is True
    data = payload["data"]
    assert data["status"] == "downloaded"
    assert data["effective_instruction"] == "旅行感、轻快、适合航拍"
    assert "后续剪辑时会一并上传" in data["user_message"]
    assert "Do not call cassette_list_assets" in data["hermes_next_step"]


def test_ingest_gateway_media_binds_confirmation_to_confirmed_optimized_prompt(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    source = SimpleNamespace(
        platform=SimpleNamespace(value="weixin"),
        chat_id="wxid_chat_raw",
        user_id="wxid_user_raw",
    )
    gateway = SimpleNamespace(_is_user_authorized=lambda _: True)
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[str(media)],
            media_types=["video/mp4"],
            text="",
            message_id="raw_message_id",
        ),
        gateway=gateway,
    )
    result = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="确认",
            message_id="confirm_message_id",
        ),
        gateway=gateway,
    )

    assert result is not None
    assert result["action"] == "rewrite"
    assert "optimized prompt confirmed" in result["text"]
    assert "Use that confirmed optimized brief as the instruction" in result["text"]
    assert "cassette_list_assets" in result["text"]
    assert "cassette_make_prompt" in result["text"]
    assert "cassette_run_job" in result["text"]
    assert "prompt=cassette_make_prompt.data.prompt" in result["text"]
    assert "chat_message=cassette_make_prompt.data.chat_message" in result["text"]
    assert "progress screenshot notifications" in result["text"]
    assert "gateway delivery behavior" in result["text"]
    assert "Do not use the short confirmation word itself" in result["text"]
    assert "wxid_chat_raw" not in result["text"]


def test_ingest_gateway_media_supports_qq_delivery_target(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    sent = []
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"),
        chat_id="qq_openid_raw",
        user_id="qq_user_raw",
        chat_type="dm",
    )
    event = SimpleNamespace(
        source=source,
        media_urls=[str(media)],
        media_types=["video/mp4"],
        text="",
        message_id="qq_message_id",
    )
    gateway = SimpleNamespace(
        _is_user_authorized=lambda _: True,
        adapters={"qqbot": SimpleNamespace(send=lambda chat_id, text: sent.append((chat_id, text)))},
    )

    result = tools.ingest_gateway_media(event=event, gateway=gateway)

    assert result is not None
    assert result["action"] == "skip"
    assert result["reply_sent"] is True
    assert sent[0][0] == "qq_openid_raw"
    assert "已保存素材 1 个" in sent[0][1]
    assert "qq_openid_raw" not in json.dumps(result, ensure_ascii=False)
    manifests = list(Path(cassette_env["asset_root"]).glob("sessions/*/manifest.json"))
    manifest_data = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest_data["delivery"]["platform"] == "qqbot"
    assert manifest_data["delivery"]["chat_id"] == "qq_openid_raw"
    assert manifest_data["delivery"]["chat_type"] == "dm"


def test_ingest_gateway_media_supports_telegram_delivery_target_and_english_default(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    sent = []

    def send(chat_id, text, metadata=None):
        sent.append((chat_id, text, metadata))

    source = SimpleNamespace(
        platform=SimpleNamespace(value="telegram"),
        chat_id="telegram_chat_raw",
        user_id="telegram_user_raw",
        thread_id="telegram_thread_raw",
    )
    gateway = SimpleNamespace(
        _is_user_authorized=lambda _: True,
        adapters={"telegram": SimpleNamespace(send=send)},
    )

    saved = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[str(media)],
            media_types=["video/mp4"],
            text="",
            message_id="telegram_message_raw",
        ),
        gateway=gateway,
    )

    assert saved is not None
    assert saved["action"] == "skip"
    assert saved["reason"] == "cassette_media_saved_waiting_for_instruction"
    assert saved["reply_sent"] is True
    assert sent[0] == (
        "telegram_chat_raw",
        "Saved 1 asset. Send more media, or send an edit instruction and I will hand it to Cassette.",
        {"thread_id": "telegram_thread_raw"},
    )
    assert "telegram_chat_raw" not in json.dumps(saved, ensure_ascii=False)
    manifest_data = json.loads(
        next(Path(cassette_env["asset_root"]).glob("sessions/*/manifest.json")).read_text(encoding="utf-8")
    )
    assert manifest_data["delivery"]["platform"] == "telegram"
    assert manifest_data["delivery"]["chat_id"] == "telegram_chat_raw"
    assert manifest_data["delivery"]["thread_id"] == "telegram_thread_raw"

    instruction = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="Make a 5 second reel with clean subtitles",
            message_id="telegram_instruction_raw",
        ),
        gateway=gateway,
    )

    _assert_semantic_edit_gate(instruction, "Make a 5 second reel with clean subtitles", language="en")
    assert len(sent) == 1
    assert "telegram_chat_raw" not in json.dumps(instruction, ensure_ascii=False)


def test_ingest_gateway_media_reads_telegram_raw_cached_attachment(cassette_env):
    audio = cassette_env["source_root"] / "song.mp3"
    audio.write_bytes(b"audio")
    sent = []

    def send(chat_id, text, metadata=None):
        sent.append((chat_id, text, metadata))

    source = SimpleNamespace(
        platform=SimpleNamespace(value="telegram"),
        chat_id="telegram_chat_raw",
        user_id="telegram_user_raw",
    )
    gateway = SimpleNamespace(
        _is_user_authorized=lambda _: True,
        adapters={"telegram": SimpleNamespace(send=send)},
    )

    result = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="[Attachment: audio]",
            message_id="telegram_audio_raw",
            raw_message={
                "document": {
                    "mime_type": "audio/mpeg",
                    "file_name": "song.mp3",
                    "file_path": str(audio),
                }
            },
        ),
        gateway=gateway,
    )

    assert result is not None
    assert result["action"] == "skip"
    assert result["reason"] == "cassette_media_saved_waiting_for_instruction"
    assert "Saved 1 asset" in sent[0][1]
    listed = json.loads(tools.cassette_list_assets({"session_id": result["session_id"]}))
    assert listed["data"]["manifest"]["assets"][0]["media_type"] == "audio"
    assert "telegram_chat_raw" not in json.dumps(result, ensure_ascii=False)


def test_gateway_language_command_overrides_qq_default_for_session(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    sent = []
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"),
        chat_id="qq_openid_raw",
        user_id="qq_user_raw",
        chat_type="dm",
    )
    gateway = SimpleNamespace(
        _is_user_authorized=lambda _: True,
        adapters={"qqbot": SimpleNamespace(send=lambda chat_id, text: sent.append((chat_id, text)))},
    )

    set_language = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="/cassette language en",
            message_id="language_message_id",
        ),
        gateway=gateway,
    )

    assert set_language is not None
    assert set_language["action"] == "skip"
    assert set_language["reason"] == "cassette_language_set"
    assert set_language["cassette_language"] == "en"
    assert "Cassette language set to English" in sent[-1][1]

    saved = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[str(media)],
            media_types=["video/mp4"],
            text="",
            message_id="raw_message_id",
        ),
        gateway=gateway,
    )

    assert saved is not None
    assert saved["action"] == "skip"
    assert "Saved 1 asset" in sent[-1][1]


def test_handle_cassette_command_language_help():
    assert "language [zh|en]" in tools.handle_cassette_command("help")
    assert "/cassette language en" in tools.handle_cassette_command("language en")
    assert "Unsupported Cassette language" in tools.handle_cassette_command("language fr")


def test_ingest_gateway_media_downloads_qq_raw_video_attachment_in_plugin(cassette_env, monkeypatch):
    downloaded = cassette_env["source_root"] / "qq_downloaded_video.mp4"
    downloaded.write_bytes(b"qq-video")
    calls = []

    def fake_download(att, gateway, event):
        calls.append((att, gateway, event))
        return str(downloaded), "video/mp4"

    monkeypatch.setattr(tools, "_download_qq_attachment_to_cache", fake_download)
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"),
        chat_id="qq_openid_raw",
        user_id="qq_user_raw",
        chat_type="dm",
    )
    event = SimpleNamespace(
        source=source,
        media_urls=[],
        media_types=[],
        text="",
        message_id="qq_message_id",
        raw_message={
            "attachments": [
                {
                    "content_type": "video/mp4",
                    "url": "https://multimedia.nt.qq.com.cn/download?token=secret",
                    "filename": "raw-user-video.mp4",
                }
            ]
        },
    )
    gateway = SimpleNamespace(_is_user_authorized=lambda _: True)

    result = tools.ingest_gateway_media(event=event, gateway=gateway)

    assert result is not None
    assert result["action"] == "skip"
    assert result["reason"] == "cassette_media_saved_waiting_for_instruction"
    serialized = json.dumps(result, ensure_ascii=False)
    assert "multimedia.nt.qq.com.cn" not in serialized
    assert "raw-user-video.mp4" not in serialized
    assert len(calls) == 1
    manifests = list(Path(cassette_env["asset_root"]).glob("sessions/*/manifest.json"))
    manifest_data = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest_data["delivery"]["platform"] == "qqbot"
    assert manifest_data["delivery"]["chat_id"] == "qq_openid_raw"
    assert manifest_data["assets"][0]["media_type"] == "video"


def test_ingest_gateway_media_treats_qq_placeholder_text_as_media_only(cassette_env):
    media = cassette_env["source_root"] / "mac-qq-video.mp4"
    media.write_bytes(b"qq-video")
    sent = []
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"),
        chat_id="qq_openid_raw",
        user_id="qq_user_raw",
        chat_type="dm",
    )
    gateway = SimpleNamespace(
        _is_user_authorized=lambda _: True,
        adapters={"qqbot": SimpleNamespace(send=lambda chat_id, text: sent.append((chat_id, text)))},
    )

    for text in ("[视频]", "视频", "[CQ:video,file=mac-qq-video.mp4]", "mac-qq-video.mp4"):
        result = tools.ingest_gateway_media(
            event=SimpleNamespace(
                source=source,
                media_urls=[str(media)],
                media_types=["video/mp4"],
                text=text,
                message_id=f"qq_media_{text}",
            ),
            gateway=gateway,
        )
        assert result is not None
        assert result["action"] == "skip"
        assert result["reason"] == "cassette_media_saved_waiting_for_instruction"

    assert sent
    assert all("已保存" in item[1] for item in sent)
    assert all("是否需要我先把你的剪辑指令优化" not in item[1] for item in sent)


def test_ingest_gateway_media_with_real_edit_caption_still_requests_optimization(cassette_env):
    media = cassette_env["source_root"] / "captioned-video.mp4"
    media.write_bytes(b"qq-video")
    sent = []
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"),
        chat_id="qq_openid_raw",
        user_id="qq_user_raw",
        chat_type="dm",
    )
    gateway = SimpleNamespace(
        _is_user_authorized=lambda _: True,
        adapters={"qqbot": SimpleNamespace(send=lambda chat_id, text: sent.append((chat_id, text)))},
    )

    result = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[str(media)],
            media_types=["video/mp4"],
            text="把这个视频剪成 10 秒，加一句字幕",
            message_id="qq_media_with_caption",
        ),
        gateway=gateway,
    )

    _assert_semantic_edit_gate(result, "把这个视频剪成 10 秒，加一句字幕")
    assert sent == []


def test_ingest_gateway_media_ignores_placeholder_without_new_media(cassette_env):
    media = cassette_env["source_root"] / "saved-video.mp4"
    media.write_bytes(b"qq-video")
    sent = []
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"),
        chat_id="qq_openid_raw",
        user_id="qq_user_raw",
        chat_type="dm",
    )
    gateway = SimpleNamespace(
        _is_user_authorized=lambda _: True,
        adapters={"qqbot": SimpleNamespace(send=lambda chat_id, text: sent.append((chat_id, text)))},
    )
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[str(media)],
            media_types=["video/mp4"],
            text="",
            message_id="qq_media_saved",
        ),
        gateway=gateway,
    )

    result = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="视频",
            message_id="qq_placeholder_only",
        ),
        gateway=gateway,
    )

    assert result is not None
    assert result["action"] == "skip"
    assert result["reason"] == "cassette_media_placeholder_ignored"
    assert "是否需要我先把你的剪辑指令优化" not in sent[-1][1]


def test_ingest_gateway_media_saves_multiple_qq_raw_media_then_binds_instruction(cassette_env, monkeypatch):
    files = {
        "first.mp4": ("video/mp4", cassette_env["source_root"] / "first.mp4"),
        "second.mp4": ("video/mp4", cassette_env["source_root"] / "second.mp4"),
        "third.mp4": ("video/mp4", cassette_env["source_root"] / "third.mp4"),
        "music.mp3": ("audio/mpeg", cassette_env["source_root"] / "music.mp3"),
        "cover.png": ("image/png", cassette_env["source_root"] / "cover.png"),
    }
    for name, (_, path) in files.items():
        path.write_bytes(f"payload-{name}".encode("utf-8"))
    sent = []

    def fake_download(att, gateway, event):
        mime, path = files[att["filename"]]
        return str(path), mime

    monkeypatch.setattr(tools, "_download_qq_attachment_to_cache", fake_download)
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"),
        chat_id="qq_openid_raw",
        user_id="qq_user_raw",
        chat_type="dm",
    )
    gateway = SimpleNamespace(
        _is_user_authorized=lambda _: True,
        adapters={"qqbot": SimpleNamespace(send=lambda chat_id, text: sent.append((chat_id, text)))},
    )
    media_event = SimpleNamespace(
        source=source,
        media_urls=[],
        media_types=[],
        text="[Attachment: media]",
        message_id="qq_media_message",
        raw_message={
            "attachments": [
                {"content_type": "video/mp4", "url": "https://multimedia.nt.qq.com.cn/1", "filename": "first.mp4"},
                {"content_type": "video/mp4", "url": "https://multimedia.nt.qq.com.cn/2", "filename": "second.mp4"},
                {"content_type": "video/mp4", "url": "https://multimedia.nt.qq.com.cn/3", "filename": "third.mp4"},
                {"content_type": "file", "url": "https://grouptalk.c2c.qq.com/4", "filename": "music.mp3"},
                {"content_type": "image/png", "url": "https://gchat.qpic.cn/5", "filename": "cover.png"},
            ]
        },
    )

    saved = tools.ingest_gateway_media(event=media_event, gateway=gateway)

    assert saved is not None
    assert saved["action"] == "skip"
    assert saved["asset_count"] == 5
    assert saved["total_asset_count"] == 5
    assert saved["reply_sent"] is True
    assert "已保存素材 5 个" in sent[0][1]
    listed = json.loads(tools.cassette_list_assets({"session_id": saved["session_id"]}))
    media_types = [asset["media_type"] for asset in listed["data"]["manifest"]["assets"]]
    assert media_types.count("video") == 3
    assert media_types.count("audio") == 1
    assert media_types.count("image") == 1

    instruction = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="把这些素材剪成一个 10 秒短视频，加配乐和封面",
            message_id="qq_instruction",
        ),
        gateway=gateway,
    )

    _assert_semantic_edit_gate(instruction, "把这些素材剪成一个 10 秒短视频，加配乐和封面", asset_count=5)
    assert len(sent) == 1
    assert "qq_openid_raw" not in json.dumps(instruction, ensure_ascii=False)


def test_ingest_gateway_media_reports_qq_raw_download_failure_without_raw_url(cassette_env, monkeypatch):
    def fake_download(att, gateway, event):
        raise tools.CassetteError("qq_attachment_download_failed", "failed")

    monkeypatch.setattr(tools, "_download_qq_attachment_to_cache", fake_download)
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"),
        chat_id="qq_openid_raw",
        user_id="qq_user_raw",
    )
    event = SimpleNamespace(
        source=source,
        media_urls=[],
        media_types=[],
        text="",
        message_id="qq_message_id",
        raw_message={
            "attachments": [
                {
                    "content_type": "video/mp4",
                    "url": "https://multimedia.nt.qq.com.cn/download?token=secret",
                    "filename": "raw-user-video.mp4",
                }
            ]
        },
    )
    gateway = SimpleNamespace(_is_user_authorized=lambda _: True)

    result = tools.ingest_gateway_media(event=event, gateway=gateway)

    assert result is not None
    assert result["action"] == "skip"
    assert result["reason"] == "cassette_media_ingest_failed"
    assert result["errors"] == ["qq_attachment_download_failed"]
    serialized = json.dumps(result, ensure_ascii=False)
    assert "multimedia.nt.qq.com.cn" not in serialized
    assert "raw-user-video.mp4" not in serialized


def test_ingest_gateway_media_is_scoped_to_hermes_session_after_new(cassette_env):
    class FakeSessionStore:
        session_id = "20260512_120000_old"

        def get_or_create_session(self, source):
            return SimpleNamespace(session_id=self.session_id)

    media_a = cassette_env["source_root"] / "old.mp4"
    media_b = cassette_env["source_root"] / "new.mp4"
    media_a.write_bytes(b"old-video")
    media_b.write_bytes(b"new-video")
    source = SimpleNamespace(
        platform=SimpleNamespace(value="weixin"),
        chat_id="wxid_same_chat",
        user_id="wxid_same_user",
    )
    gateway = SimpleNamespace(_is_user_authorized=lambda _: True)
    session_store = FakeSessionStore()

    first = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[str(media_a)],
            media_types=["video/mp4"],
            text="",
            message_id="old_message",
        ),
        gateway=gateway,
        session_store=session_store,
    )
    old_cassette_session_id = first["session_id"]

    session_store.session_id = "20260512_122211_3b9877d1"
    second = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[str(media_b)],
            media_types=["video/mp4"],
            text="",
            message_id="new_message",
        ),
        gateway=gateway,
        session_store=session_store,
    )
    new_cassette_session_id = second["session_id"]

    assert new_cassette_session_id != old_cassette_session_id
    old_assets = json.loads(tools.cassette_list_assets({"session_id": old_cassette_session_id}))
    new_assets = json.loads(tools.cassette_list_assets({"session_id": new_cassette_session_id}))
    assert len(old_assets["data"]["manifest"]["assets"]) == 1
    assert len(new_assets["data"]["manifest"]["assets"]) == 1
    assert new_assets["data"]["manifest"]["assets"][0]["original_name"] == "new.mp4"

    instruction = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="剪成 3 秒",
            message_id="new_instruction",
        ),
        gateway=gateway,
        session_store=session_store,
    )
    assert instruction is not None
    _assert_semantic_edit_gate(instruction, "剪成 3 秒")
    assert "do not inspect" in instruction["text"].lower()
    assert "terminal" in instruction["text"]
    assert "ffmpeg" in instruction["text"]
    assert old_cassette_session_id not in instruction["text"]
    assert f"session_id `{new_cassette_session_id}`" in instruction["text"]


def test_ingest_gateway_media_does_not_bind_greeting_to_saved_assets(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    source = SimpleNamespace(
        platform=SimpleNamespace(value="weixin"),
        chat_id="wxid_chat_raw",
        user_id="wxid_user_raw",
    )
    gateway = SimpleNamespace(_is_user_authorized=lambda _: True)
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[str(media)],
            media_types=["video/mp4"],
            text="",
            message_id="raw_message_id",
        ),
        gateway=gateway,
    )
    greeting_event = SimpleNamespace(
        source=source,
        media_urls=[],
        media_types=[],
        text="你好",
        message_id="raw_text_message_id",
    )

    assert tools.ingest_gateway_media(event=greeting_event, gateway=gateway) is None


def test_ingest_gateway_media_asks_hermes_to_classify_ambiguous_followup(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    source = SimpleNamespace(
        platform=SimpleNamespace(value="weixin"),
        chat_id="wxid_chat_raw",
        user_id="wxid_user_raw",
    )
    gateway = SimpleNamespace(_is_user_authorized=lambda _: True)
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[str(media)],
            media_types=["video/mp4"],
            text="",
            message_id="raw_message_id",
        ),
        gateway=gateway,
    )
    session_id = tools._gateway_session_id(SimpleNamespace(source=source), None)

    result = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="按这个感觉来",
            message_id="ambiguous_text_message_id",
        ),
        gateway=gateway,
    )

    assert result is not None
    assert result["action"] == "rewrite"
    assert result["text"].startswith("按这个感觉来")
    assert "Hermes must semantically decide" in result["text"]
    assert "是否需要我先把你的剪辑指令优化" in result["text"]
    pending = tools._load_pending_edit(session_id)
    assert pending["state"] == "awaiting_optimization_choice"
    assert pending["semantic_gate"] is True


def test_ingest_gateway_media_clears_semantic_gate_on_unrelated_followup(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    source = SimpleNamespace(
        platform=SimpleNamespace(value="weixin"),
        chat_id="wxid_chat_raw",
        user_id="wxid_user_raw",
    )
    gateway = SimpleNamespace(_is_user_authorized=lambda _: True)
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source, media_urls=[str(media)], media_types=["video/mp4"], text="", message_id="raw_message_id"
        ),
        gateway=gateway,
    )
    session_id = tools._gateway_session_id(SimpleNamespace(source=source), None)
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source, media_urls=[], media_types=[], text="按这个感觉来", message_id="ambiguous_text_message_id"
        ),
        gateway=gateway,
    )

    result = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source, media_urls=[], media_types=[], text="谢谢", message_id="thanks_message_id"
        ),
        gateway=gateway,
    )

    assert result is None
    assert tools._load_pending_edit(session_id) is None


def test_asset_status_query_detection():
    assert tools._looks_like_asset_status_query("Did you receive the video files?") is False
    assert tools._looks_like_asset_status_query("/check_assets") is True
    assert tools._looks_like_asset_status_query("/check_assets@CassetteBot") is True


def test_gateway_bgm_replacement_instruction_uses_saved_assets(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"),
        chat_id="qq_openid_raw",
        user_id="qq_user_raw",
        chat_type="dm",
    )
    gateway = SimpleNamespace(_is_user_authorized=lambda _: True)
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[str(media)],
            media_types=["video/mp4"],
            text="",
            message_id="raw_message_id",
        ),
        gateway=gateway,
    )

    result = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="把背景音乐替换成恢弘大气的坐飞机的音乐",
            message_id="raw_text_message_id",
        ),
        gateway=gateway,
    )

    _assert_semantic_edit_gate(result, "把背景音乐替换成恢弘大气的坐飞机的音乐")
    assert "qq_openid_raw" not in result["text"]


def test_gateway_edit_slash_command_forces_saved_asset_edit_flow(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"),
        chat_id="qq_openid_raw",
        user_id="qq_user_raw",
        chat_type="dm",
    )
    gateway = SimpleNamespace(_is_user_authorized=lambda _: True)
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[str(media)],
            media_types=["video/mp4"],
            text="",
            message_id="raw_message_id",
        ),
        gateway=gateway,
    )

    result = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="/edit 按这个感觉来",
            message_id="raw_text_message_id",
        ),
        gateway=gateway,
    )

    assert result is not None
    assert result["action"] == "rewrite"
    assert result["text"].startswith("按这个感觉来")
    assert "/edit" not in result["text"].split("\n", 1)[0]
    assert "Cassette gateway assets available: 1 asset(s)" in result["text"]
    assert "whether to use the prompt optimization feature" in result["text"]
    assert "qq_openid_raw" not in result["text"]


def test_gateway_edit_slash_command_without_assets_gets_fixed_reply(cassette_env):
    sent = []
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"),
        chat_id="qq_openid_raw",
        user_id="qq_user_raw",
        chat_type="dm",
    )
    gateway = SimpleNamespace(
        _is_user_authorized=lambda _: True,
        adapters={"qqbot": SimpleNamespace(send=lambda chat_id, text: sent.append((chat_id, text)))},
    )

    result = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="/edit 按这个感觉来",
            message_id="raw_text_message_id",
        ),
        gateway=gateway,
    )

    assert result is not None
    assert result["action"] == "skip"
    assert result["reason"] == "cassette_edit_command_missing_assets"
    assert sent[-1] == (
        "qq_openid_raw",
        "还没有可用素材。请先发送视频、图片或音频素材，再用 /edit 加剪辑指令触发 Cassette。",
    )
    assert "qq_openid_raw" not in json.dumps(result, ensure_ascii=False)


def test_gateway_first_edit_requests_cassette_model_choice_once(cassette_env, monkeypatch):
    monkeypatch.setenv("CASSETTE_GATEWAY_MODEL_CHOICE_ENABLED", "1")
    monkeypatch.setattr(
        tools.browser,
        "fetch_cassette_model_options",
        lambda language="zh": {
            "models": [{"label": "DeepSeek V4 Flash"}, {"label": "Kimi K2.6"}],
            "thinking_levels": [
                {"label": "低", "value": "Low"},
                {"label": "中", "value": "Medium"},
                {"label": "高", "value": "High"},
            ],
            "source": "cassette_agent_page",
            "language": language,
        },
    )
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    sent = []
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"), chat_id="qq_openid_raw", user_id="qq_user_raw", chat_type="dm"
    )
    gateway = SimpleNamespace(
        _is_user_authorized=lambda _: True,
        adapters={"qqbot": SimpleNamespace(send=lambda chat_id, text: sent.append((chat_id, text)))},
    )
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source, media_urls=[str(media)], media_types=["video/mp4"], text="", message_id="raw_message_id"
        ),
        gateway=gateway,
    )
    session_id = tools._gateway_session_id(SimpleNamespace(source=source), None)

    requested = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source, media_urls=[], media_types=[], text="/edit 剪成10秒短片", message_id="edit_message_id"
        ),
        gateway=gateway,
    )
    assert requested is not None
    assert requested["reason"] == "cassette_model_choice_requested"
    assert "请选择当前 Cassette 会话使用的模型" in sent[-1][1]
    assert "1. DeepSeek V4 Flash" in sent[-1][1]
    assert "2. Kimi K2.6" in sent[-1][1]

    model_choice = tools.ingest_gateway_media(
        event=SimpleNamespace(source=source, media_urls=[], media_types=[], text="2", message_id="model_choice"),
        gateway=gateway,
    )
    assert model_choice is not None
    assert model_choice["reason"] == "cassette_model_thinking_choice_requested"
    assert "已选择模型：Kimi K2.6" in sent[-1][1]
    assert "3. 高" in sent[-1][1]

    thinking_choice = tools.ingest_gateway_media(
        event=SimpleNamespace(source=source, media_urls=[], media_types=[], text="3", message_id="thinking_choice"),
        gateway=gateway,
    )
    assert thinking_choice is not None
    assert thinking_choice["reason"] == "cassette_prompt_optimization_choice_requested"
    prefs = tools._load_session_preferences(session_id)
    assert prefs["cassette_model"] == "Kimi K2.6"
    assert prefs["cassette_thinking_level"] == "High"
    assert prefs["cassette_model_selection_completed"] is True


def test_gateway_model_choice_rejects_non_choice_without_semantic_fallback(cassette_env, monkeypatch):
    monkeypatch.setenv("CASSETTE_GATEWAY_MODEL_CHOICE_ENABLED", "1")
    monkeypatch.setattr(
        tools.browser,
        "fetch_cassette_model_options",
        lambda language="zh": {
            "models": [{"label": "DeepSeek V4 Flash"}, {"label": "Kimi K2.6"}],
            "thinking_levels": [{"label": "低", "value": "Low"}],
            "source": "cassette_agent_page",
            "language": language,
        },
    )
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    sent = []
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"), chat_id="qq_openid_raw", user_id="qq_user_raw", chat_type="dm"
    )
    gateway = SimpleNamespace(
        _is_user_authorized=lambda _: True,
        adapters={"qqbot": SimpleNamespace(send=lambda chat_id, text: sent.append((chat_id, text)))},
    )
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source, media_urls=[str(media)], media_types=["video/mp4"], text="", message_id="raw_message_id"
        ),
        gateway=gateway,
    )
    session_id = tools._gateway_session_id(SimpleNamespace(source=source), None)
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source, media_urls=[], media_types=[], text="剪成10秒短片", message_id="semantic_edit_message_id"
        ),
        gateway=gateway,
    )

    result = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source, media_urls=[], media_types=[], text="匹配一个大气的音乐", message_id="bad_choice"
        ),
        gateway=gateway,
    )

    assert result is not None
    assert result["reason"] == "cassette_model_choice_busy_rejected"
    assert sent[-1][1] == "请使用/cut命令终止当前流程或剪辑任务后再尝试开始新的剪辑任务"
    assert tools._load_pending_edit(session_id)["state"] == "awaiting_model_choice"


def test_gateway_cut_slash_command_clears_pending_model_choice(cassette_env, monkeypatch):
    monkeypatch.setenv("CASSETTE_GATEWAY_MODEL_CHOICE_ENABLED", "1")
    monkeypatch.setattr(
        tools.browser,
        "fetch_cassette_model_options",
        lambda language="zh": {
            "models": [{"label": "DeepSeek V4 Flash"}],
            "thinking_levels": [{"label": "低", "value": "Low"}],
            "source": "cassette_agent_page",
            "language": language,
        },
    )
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    sent = []
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"), chat_id="qq_openid_raw", user_id="qq_user_raw", chat_type="dm"
    )
    gateway = SimpleNamespace(
        _is_user_authorized=lambda _: True,
        adapters={"qqbot": SimpleNamespace(send=lambda chat_id, text: sent.append((chat_id, text)))},
    )
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source, media_urls=[str(media)], media_types=["video/mp4"], text="", message_id="raw_message_id"
        ),
        gateway=gateway,
    )
    session_id = tools._gateway_session_id(SimpleNamespace(source=source), None)
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source, media_urls=[], media_types=[], text="/edit 剪成10秒短片", message_id="edit_message_id"
        ),
        gateway=gateway,
    )
    assert tools._load_pending_edit(session_id)["state"] == "awaiting_model_choice"

    result = tools.ingest_gateway_media(
        event=SimpleNamespace(source=source, media_urls=[], media_types=[], text="/cut", message_id="cut_message_id"),
        gateway=gateway,
    )

    assert result is not None
    assert result["reason"] == "cassette_cut_requested"
    assert result["pending_state"] == "awaiting_model_choice"
    assert tools._load_pending_edit(session_id) is None
    assert "已请求停止当前 Cassette 操作" in sent[-1][1]


def test_gateway_semantic_first_edit_requests_cassette_model_choice_before_bgm(cassette_env, monkeypatch):
    monkeypatch.setenv("CASSETTE_GATEWAY_MODEL_CHOICE_ENABLED", "1")
    monkeypatch.setattr(
        tools.browser,
        "fetch_cassette_model_options",
        lambda language="zh": {
            "models": [{"label": "DeepSeek V4 Flash"}, {"label": "Kimi K2.6"}],
            "thinking_levels": [
                {"label": "低", "value": "Low"},
                {"label": "中", "value": "Medium"},
                {"label": "高", "value": "High"},
            ],
            "source": "cassette_agent_page",
            "language": language,
        },
    )
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    sent = []
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"), chat_id="qq_openid_raw", user_id="qq_user_raw", chat_type="dm"
    )
    gateway = SimpleNamespace(
        _is_user_authorized=lambda _: True,
        adapters={"qqbot": SimpleNamespace(send=lambda chat_id, text: sent.append((chat_id, text)))},
    )
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source, media_urls=[str(media)], media_types=["video/mp4"], text="", message_id="raw_message_id"
        ),
        gateway=gateway,
    )
    session_id = tools._gateway_session_id(SimpleNamespace(source=source), None)

    semantic_gate = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source, media_urls=[], media_types=[], text="剪成10秒短片", message_id="semantic_edit_message_id"
        ),
        gateway=gateway,
    )
    _assert_semantic_edit_gate(semantic_gate, "剪成10秒短片", expect_prompt_optimization=False)
    assert "请选择当前 Cassette 会话使用的模型" in semantic_gate["text"]
    assert "是否需要我先把你的剪辑指令优化" not in semantic_gate["text"]
    assert "Do not call cassette_list_assets" in semantic_gate["text"]

    model_choice = tools.ingest_gateway_media(
        event=SimpleNamespace(source=source, media_urls=[], media_types=[], text="2", message_id="model_choice"),
        gateway=gateway,
    )
    assert model_choice is not None
    assert model_choice["reason"] == "cassette_model_thinking_choice_requested"
    assert "已选择模型：Kimi K2.6" in sent[-1][1]

    thinking_choice = tools.ingest_gateway_media(
        event=SimpleNamespace(source=source, media_urls=[], media_types=[], text="3", message_id="thinking_choice"),
        gateway=gateway,
    )
    assert thinking_choice is not None
    assert thinking_choice["reason"] == "cassette_prompt_optimization_choice_requested"
    assert "是否需要我先把你的剪辑指令优化" in sent[-1][1]

    declined = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source, media_urls=[], media_types=[], text="不优化", message_id="optimization_choice"
        ),
        gateway=gateway,
    )
    assert declined is not None
    assert declined["reason"] == "cassette_smart_bgm_choice_requested"
    assert "是否需要我根据剪辑指令智能匹配一首 BGM" in sent[-1][1]
    pending = tools._load_pending_edit(session_id)
    assert pending["state"] == "awaiting_bgm_choice"
    assert pending["optimization_enabled"] is False
    prefs = tools._load_session_preferences(session_id)
    assert prefs["cassette_model"] == "Kimi K2.6"
    assert prefs["cassette_thinking_level"] == "High"
    assert prefs["cassette_model_selection_completed"] is True

    bgm_declined = tools.ingest_gateway_media(
        event=SimpleNamespace(source=source, media_urls=[], media_types=[], text="不需要 BGM", message_id="bgm_choice"),
        gateway=gateway,
    )
    assert bgm_declined is not None
    assert "prompt optimization declined" in bgm_declined["text"]

    followup = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source, media_urls=[], media_types=[], text="把字幕换成黄色", message_id="followup_edit"
        ),
        gateway=gateway,
    )
    assert followup is not None
    assert followup["action"] == "rewrite"
    assert followup["text"].startswith("把字幕换成黄色")
    assert "follow-up edit" in followup["text"]
    assert "请选择当前 Cassette 会话使用的模型" not in followup["text"]
    assert "whether to use the prompt optimization feature" not in followup["text"]
    assert "是否需要我先把你的剪辑指令优化" not in followup["text"]


def test_gateway_cassette_model_command_sets_preference_without_assets(cassette_env, monkeypatch):
    monkeypatch.setattr(
        tools.browser,
        "fetch_cassette_model_options",
        lambda language="zh": {
            "models": [{"label": "DeepSeek V4 Flash"}, {"label": "MiMo V2.5 Pro"}],
            "thinking_levels": [{"label": "低", "value": "Low"}, {"label": "高", "value": "High"}],
            "source": "cassette_agent_page",
            "language": language,
        },
    )
    sent = []
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"), chat_id="qq_openid_raw", user_id="qq_user_raw", chat_type="dm"
    )
    gateway = SimpleNamespace(
        _is_user_authorized=lambda _: True,
        adapters={"qqbot": SimpleNamespace(send=lambda chat_id, text: sent.append((chat_id, text)))},
    )
    session_id = tools._gateway_session_id(SimpleNamespace(source=source), None)

    result = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source, media_urls=[], media_types=[], text="/cassette_model", message_id="model_command"
        ),
        gateway=gateway,
    )
    assert result is not None
    assert result["reason"] == "cassette_model_choice_requested"
    assert "MiMo V2.5 Pro" in sent[-1][1]

    tools.ingest_gateway_media(
        event=SimpleNamespace(source=source, media_urls=[], media_types=[], text="2", message_id="model_choice"),
        gateway=gateway,
    )
    result = tools.ingest_gateway_media(
        event=SimpleNamespace(source=source, media_urls=[], media_types=[], text="2", message_id="thinking_choice"),
        gateway=gateway,
    )
    assert result is not None
    assert result["reason"] == "cassette_model_set"
    prefs = tools._load_session_preferences(session_id)
    assert prefs["cassette_model"] == "MiMo V2.5 Pro"
    assert prefs["cassette_thinking_level"] == "High"


def test_gateway_model_choice_does_not_fallback_to_hardcoded_options(cassette_env, monkeypatch):
    def fail_fetch(language="zh"):
        raise RuntimeError("cassette_unreachable")

    monkeypatch.setattr(tools.browser, "fetch_cassette_model_options", fail_fetch)
    sent = []
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"), chat_id="qq_openid_raw", user_id="qq_user_raw", chat_type="dm"
    )
    gateway = SimpleNamespace(
        _is_user_authorized=lambda _: True,
        adapters={"qqbot": SimpleNamespace(send=lambda chat_id, text: sent.append((chat_id, text)))},
    )

    result = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source, media_urls=[], media_types=[], text="/cassette_model", message_id="model_command"
        ),
        gateway=gateway,
    )

    assert result is not None
    assert result["reason"] == "cassette_model_options_unavailable"
    assert "无法从 Cassette 页面获取模型列表" in sent[-1][1]
    assert "DeepSeek" not in sent[-1][1]
    assert "Kimi" not in sent[-1][1]


def test_gateway_cut_slash_command_requests_active_job_cancel(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    sent = []
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"),
        chat_id="qq_openid_raw",
        user_id="qq_user_raw",
        chat_type="dm",
    )
    gateway = SimpleNamespace(
        _is_user_authorized=lambda _: True,
        adapters={"qqbot": SimpleNamespace(send=lambda chat_id, text: sent.append((chat_id, text)))},
    )
    media_event = SimpleNamespace(
        source=source,
        media_urls=[str(media)],
        media_types=["video/mp4"],
        text="",
        message_id="raw_message_id",
    )
    tools.ingest_gateway_media(event=media_event, gateway=gateway)
    session_id = tools._gateway_session_id(media_event)
    sess_hash = tools.manifest.resolve_session_hash(session_id=session_id)
    job = jobs.create_job(
        sess_hash,
        "internal",
        "instruction",
        [str(media)],
        {"cassette_session_id": session_id},
    )
    job["status"] = "running"
    job["started_at"] = jobs.now_iso()
    job["current_stage"] = "agent"
    jobs.save_job(job)

    result = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="/cut",
            message_id="raw_cut_message_id",
        ),
        gateway=gateway,
    )

    assert result is not None
    assert result["action"] == "skip"
    assert result["reason"] == "cassette_cut_requested"
    assert result["job_id"] == job["job_id"]
    assert jobs.load_job(job["job_id"])["status"] == "cancel_requested"
    assert "已请求停止当前 Cassette 操作" in sent[-1][1]
    assert "qq_openid_raw" not in json.dumps(result, ensure_ascii=False)


def test_gateway_active_job_rejects_new_text_instruction(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    sent = []
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"),
        chat_id="qq_openid_raw",
        user_id="qq_user_raw",
        chat_type="dm",
    )
    gateway = SimpleNamespace(
        _is_user_authorized=lambda _: True,
        adapters={"qqbot": SimpleNamespace(send=lambda chat_id, text: sent.append((chat_id, text)))},
    )
    media_event = SimpleNamespace(
        source=source,
        media_urls=[str(media)],
        media_types=["video/mp4"],
        text="",
        message_id="raw_message_id",
    )
    tools.ingest_gateway_media(event=media_event, gateway=gateway)
    session_id = tools._gateway_session_id(media_event)
    sess_hash = tools.manifest.resolve_session_hash(session_id=session_id)
    job = jobs.create_job(
        sess_hash,
        "internal",
        "instruction",
        [str(media)],
        {"cassette_session_id": session_id},
    )
    job["status"] = "running"
    job["started_at"] = jobs.now_iso()
    jobs.save_job(job)

    result = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="再剪一个版本",
            message_id="raw_second_message_id",
        ),
        gateway=gateway,
    )

    assert result is not None
    assert result["reason"] == "cassette_active_job_busy_rejected"
    assert sent[-1][1] == "请使用/cut命令终止当前流程或剪辑任务后再尝试开始新的剪辑任务"


def test_gateway_cut_slash_command_without_active_job_pauses_session(cassette_env):
    sent = []
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"),
        chat_id="qq_openid_raw",
        user_id="qq_user_raw",
        chat_type="dm",
    )
    gateway = SimpleNamespace(
        _is_user_authorized=lambda _: True,
        adapters={"qqbot": SimpleNamespace(send=lambda chat_id, text: sent.append((chat_id, text)))},
    )

    result = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="/cut",
            message_id="raw_cut_message_id",
        ),
        gateway=gateway,
    )

    assert result is not None
    assert result["action"] == "skip"
    assert result["reason"] == "cassette_cut_no_active_job"
    assert "Cassette 当前没有正在运行的剪辑任务" in sent[-1][1]
    assert "qq_openid_raw" not in json.dumps(result, ensure_ascii=False)


def test_gateway_cut_matches_active_job_when_session_store_missing(cassette_env):
    sent = []
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"),
        chat_id="qq_openid_raw",
        user_id="qq_user_raw",
        chat_type="dm",
    )
    gateway = SimpleNamespace(
        _is_user_authorized=lambda _: True,
        adapters={"qqbot": SimpleNamespace(send=lambda chat_id, text: sent.append((chat_id, text)))},
    )
    cut_event = SimpleNamespace(
        source=source,
        media_urls=[],
        media_types=[],
        text="/cut",
        message_id="raw_cut_message_id",
    )
    base_session_id = tools._gateway_session_id(cut_event)
    live_session_id = f"{base_session_id}_0123456789abcdef"
    sess_hash = tools.manifest.resolve_session_hash(session_id=live_session_id)
    job = jobs.create_job(
        sess_hash,
        "internal",
        "instruction",
        [],
        {"cassette_session_id": live_session_id},
    )
    job["status"] = "running"
    job["started_at"] = jobs.now_iso()
    job["current_stage"] = "agent"
    jobs.save_job(job)

    result = tools.ingest_gateway_media(event=cut_event, gateway=gateway)

    assert result is not None
    assert result["action"] == "skip"
    assert result["reason"] == "cassette_cut_requested"
    assert result["job_id"] == job["job_id"]
    assert jobs.load_job(job["job_id"])["status"] == "cancel_requested"
    assert "已请求停止当前 Cassette 操作" in sent[-1][1]


def test_gateway_cut_accepts_telegram_bot_command_suffix(cassette_env):
    sent = []

    def send(chat_id, text, metadata=None):
        sent.append((chat_id, text, metadata))

    source = SimpleNamespace(
        platform=SimpleNamespace(value="telegram"),
        chat_id="telegram_chat_raw",
        user_id="telegram_user_raw",
        thread_id="telegram_thread_raw",
    )
    gateway = SimpleNamespace(
        _is_user_authorized=lambda _: True,
        adapters={"telegram": SimpleNamespace(send=send)},
    )
    session_id = tools._gateway_session_id(SimpleNamespace(source=source))
    sess_hash = tools.manifest.resolve_session_hash(session_id=session_id)
    job = jobs.create_job(
        sess_hash,
        "internal",
        "instruction",
        [],
        {
            "cassette_session_id": session_id,
            "delivery": {
                "platform": "telegram",
                "chat_id": "telegram_chat_raw",
                "user_id": "telegram_user_raw",
                "thread_id": "telegram_thread_raw",
            },
        },
    )
    job["status"] = "running"
    job["current_stage"] = "agent"
    jobs.save_job(job)

    result = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="/cut@CassetteBot",
            message_id="raw_cut_message_id",
        ),
        gateway=gateway,
    )

    assert result is not None
    assert result["reason"] == "cassette_cut_requested"
    assert result["job_id"] == job["job_id"]
    assert jobs.load_job(job["job_id"])["status"] == "cancel_requested"
    assert "Requested a stop for the current Cassette operation" in sent[-1][1]
    assert sent[-1][2] == {"thread_id": "telegram_thread_raw"}
    assert "telegram_chat_raw" not in json.dumps(result, ensure_ascii=False)


def test_gateway_cut_matches_active_telegram_job_by_delivery_when_session_id_changed(cassette_env):
    sent = []

    def send(chat_id, text, metadata=None):
        sent.append((chat_id, text, metadata))

    source = SimpleNamespace(
        platform=SimpleNamespace(value="telegram"),
        chat_id="telegram_chat_raw",
        user_id="telegram_user_raw",
        thread_id="telegram_thread_raw",
    )
    gateway = SimpleNamespace(
        _is_user_authorized=lambda _: True,
        adapters={"telegram": SimpleNamespace(send=send)},
    )
    old_session_id = "gateway_media_telegram_oldchat_oldsession"
    job = jobs.create_job(
        "old-session-hash",
        "internal",
        "instruction",
        [],
        {
            "cassette_session_id": old_session_id,
            "delivery": {
                "platform": "telegram",
                "chat_id": "telegram_chat_raw",
                "user_id": "telegram_user_raw",
                "thread_id": "telegram_thread_raw",
            },
        },
    )
    job["status"] = "running"
    job["current_stage"] = "agent"
    jobs.save_job(job)

    result = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="/cut",
            message_id="raw_cut_message_id",
        ),
        gateway=gateway,
    )

    assert result is not None
    assert result["reason"] == "cassette_cut_requested"
    assert result["job_id"] == job["job_id"]
    assert jobs.load_job(job["job_id"])["status"] == "cancel_requested"
    assert sent[-1][2] == {"thread_id": "telegram_thread_raw"}


def test_cut_plugin_command_requests_latest_active_job_cancel(cassette_env):
    job = jobs.create_job(
        "session-hash",
        "internal",
        "instruction",
        [],
        {"cassette_session_id": "gateway_media_qqbot_chat_0123456789abcdef"},
    )
    job["status"] = "running"
    jobs.save_job(job)

    result = tools.handle_cut_command("")

    assert "已请求停止当前 Cassette 操作" in result
    assert jobs.load_job(job["job_id"])["status"] == "cancel_requested"


def test_gateway_cassette_cut_subcommand_requests_active_job_cancel(cassette_env):
    sent = []
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"),
        chat_id="qq_openid_raw",
        user_id="qq_user_raw",
        chat_type="dm",
    )
    gateway = SimpleNamespace(
        _is_user_authorized=lambda _: True,
        adapters={"qqbot": SimpleNamespace(send=lambda chat_id, text: sent.append((chat_id, text)))},
    )
    cut_event = SimpleNamespace(
        source=source,
        media_urls=[],
        media_types=[],
        text="/cassette cut",
        message_id="raw_cut_message_id",
    )
    session_id = tools._gateway_session_id(cut_event)
    sess_hash = tools.manifest.resolve_session_hash(session_id=session_id)
    job = jobs.create_job(
        sess_hash,
        "internal",
        "instruction",
        [],
        {"cassette_session_id": session_id},
    )
    job["status"] = "running"
    jobs.save_job(job)

    result = tools.ingest_gateway_media(event=cut_event, gateway=gateway)

    assert result is not None
    assert result["reason"] == "cassette_cut_requested"
    assert jobs.load_job(job["job_id"])["status"] == "cancel_requested"
    assert "已请求停止当前 Cassette 操作" in sent[-1][1]


def test_gateway_followup_edit_skips_initial_choice_questions(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"),
        chat_id="qq_openid_raw",
        user_id="qq_user_raw",
        chat_type="dm",
    )
    gateway = SimpleNamespace(_is_user_authorized=lambda _: True)
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[str(media)],
            media_types=["video/mp4"],
            text="",
            message_id="raw_message_id",
        ),
        gateway=gateway,
    )

    first = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="剪成 10 秒短视频，加中文字幕",
            message_id="raw_first_instruction",
        ),
        gateway=gateway,
    )
    assert first is not None
    _assert_semantic_edit_gate(first, "剪成 10 秒短视频，加中文字幕")

    declined = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="不优化",
            message_id="raw_decline",
        ),
        gateway=gateway,
    )
    assert declined is not None
    assert "smart BGM choice is required" in declined["text"]

    bgm_declined = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="不需要 BGM",
            message_id="raw_bgm_decline",
        ),
        gateway=gateway,
    )
    assert bgm_declined is not None
    assert "prompt optimization declined" in bgm_declined["text"]

    followup_instruction = "请把字幕换成黄色并重新导出"
    followup = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text=followup_instruction,
            message_id="raw_followup_instruction",
        ),
        gateway=gateway,
    )

    assert followup is not None
    assert followup["action"] == "rewrite"
    assert followup["text"].startswith(followup_instruction)
    assert "Hermes must semantically decide" in followup["text"]
    assert "follow-up edit" in followup["text"]
    assert "whether to use the prompt optimization feature" not in followup["text"]
    assert "smart BGM choice" not in followup["text"]
    assert "qq_openid_raw" not in followup["text"]


def test_gateway_retry_after_exact_bgm_selection_does_not_reask_or_rematch_bgm(cassette_env, monkeypatch):
    monkeypatch.setenv("JAMENDO_CLIENT_ID", "configured-client-id")
    tools._JAMENDO_DISABLED_CODE = None
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"),
        chat_id="qq_openid_raw",
        user_id="qq_user_raw",
        chat_type="dm",
    )
    gateway = SimpleNamespace(_is_user_authorized=lambda _: True)
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[str(media)],
            media_types=["video/mp4"],
            text="",
            message_id="raw_message_id",
        ),
        gateway=gateway,
    )

    first_instruction = "给我的视频配上一个流行歌要温柔男声"
    first = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text=first_instruction,
            message_id="raw_first_instruction",
        ),
        gateway=gateway,
    )
    assert first is not None
    _assert_semantic_edit_gate(first, first_instruction)

    declined = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="不优化",
            message_id="raw_decline",
        ),
        gateway=gateway,
    )
    assert declined is not None
    assert "smart BGM choice is required" in declined["text"]

    bgm_accepted = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="需要 BGM",
            message_id="raw_bgm_accept",
        ),
        gateway=gateway,
    )
    assert bgm_accepted is not None
    assert "recommend exactly 3 real songs" in bgm_accepted["text"]
    assert "cassette_match_exact_bgm" in bgm_accepted["text"]

    selected = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="1",
            message_id="raw_exact_choice",
        ),
        gateway=gateway,
    )
    assert selected is not None
    assert "cassette_match_exact_bgm" in selected["text"]
    assert "selected smart BGM recommendation #1" in selected["text"]
    assert 'fallback_from="exact_bgm"' in selected["text"]
    assert "fallback_reason set to the exact-song tool error code" in selected["text"]

    retry = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text=first_instruction,
            message_id="raw_retry_instruction",
        ),
        gateway=gateway,
    )

    assert retry is not None
    assert retry["action"] == "rewrite"
    assert retry["text"].startswith(first_instruction)
    assert "Hermes must semantically decide" in retry["text"]
    assert "follow-up edit" in retry["text"]
    assert "whether to use the prompt optimization feature" not in retry["text"]
    assert "smart BGM choice" not in retry["text"]
    assert "jamendo_music_matcher" not in retry["text"]
    assert "cassette_match_bgm" not in retry["text"]
    tools._JAMENDO_DISABLED_CODE = None


def test_stale_bgm_pending_does_not_overwrite_completed_initial_choices(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"),
        chat_id="qq_openid_raw",
        user_id="qq_user_raw",
        chat_type="dm",
    )
    gateway = SimpleNamespace(_is_user_authorized=lambda _: True)
    media_event = SimpleNamespace(
        source=source,
        media_urls=[str(media)],
        media_types=["video/mp4"],
        text="",
        message_id="raw_message_id",
    )
    session_id = tools._gateway_session_id(media_event)
    tools.ingest_gateway_media(event=media_event, gateway=gateway)
    tools._mark_initial_edit_choices_completed(
        session_id,
        optimization_enabled=False,
        smart_bgm_enabled=True,
        source="initial_bgm_accept",
    )
    tools._save_pending_edit(
        session_id,
        "给我的视频配上一个流行歌要温柔男声",
        1,
        "awaiting_bgm_choice",
        optimization_enabled=False,
    )

    retry = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="不需要 BGM",
            message_id="raw_retry_instruction",
        ),
        gateway=gateway,
    )

    prefs = tools._load_session_preferences(session_id)
    assert prefs["initial_choice_source"] == "initial_bgm_accept"
    assert prefs["smart_bgm_enabled"] is True
    assert tools._load_pending_edit(session_id) is None
    assert retry is not None
    assert retry["action"] == "rewrite"
    assert "follow-up edit" in retry["text"]
    assert "bgm_declined_by_user" not in retry["text"]


def test_bgm_accept_does_not_treat_full_retry_instruction_as_yes(cassette_env):
    assert tools._looks_like_bgm_accept("需要 BGM") is True
    assert tools._looks_like_bgm_accept("好的") is True
    assert tools._looks_like_bgm_accept("给我的视频配上一个流行歌要温柔男声") is False


def test_gateway_reserved_slash_command_does_not_satisfy_pending_choice(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"),
        chat_id="qq_openid_raw",
        user_id="qq_user_raw",
        chat_type="dm",
    )
    gateway = SimpleNamespace(_is_user_authorized=lambda _: True)
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[str(media)],
            media_types=["video/mp4"],
            text="",
            message_id="raw_message_id",
        ),
        gateway=gateway,
    )
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="剪成 10 秒短视频，加中文字幕",
            message_id="raw_first_instruction",
        ),
        gateway=gateway,
    )

    result = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="/new",
            message_id="raw_new_command",
        ),
        gateway=gateway,
    )

    assert result is None


def test_gateway_refine_slash_command_forces_prompt_optimization(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"),
        chat_id="qq_openid_raw",
        user_id="qq_user_raw",
        chat_type="dm",
    )
    gateway = SimpleNamespace(_is_user_authorized=lambda _: True)
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[str(media)],
            media_types=["video/mp4"],
            text="",
            message_id="raw_message_id",
        ),
        gateway=gateway,
    )

    result = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="/refine 做成高级感旅行短片",
            message_id="raw_refine_command",
        ),
        gateway=gateway,
    )

    assert result is not None
    assert result["action"] == "rewrite"
    assert result["text"].startswith("做成高级感旅行短片")
    assert "Cassette prompt optimization accepted" in result["text"]
    assert "whether to use the prompt optimization feature" not in result["text"]
    assert "qq_openid_raw" not in result["text"]


def test_gateway_music_slash_command_only_adds_bgm_material(cassette_env, monkeypatch):
    monkeypatch.setattr(
        tools,
        "_freetouse_category_summary",
        lambda: "- Travel (Video); related tags: sunny, uplifting",
    )
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"),
        chat_id="qq_openid_raw",
        user_id="qq_user_raw",
        chat_type="dm",
    )
    gateway = SimpleNamespace(_is_user_authorized=lambda _: True)

    result = tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source,
            media_urls=[],
            media_types=[],
            text="/music 旅行感、轻快、适合航拍",
            message_id="raw_music_command",
        ),
        gateway=gateway,
    )

    assert result is not None
    assert result["action"] == "rewrite"
    assert "standalone material-ingest command" in result["text"]
    assert "exactly five numbered options" in result["text"]
    assert "recommend exactly 3 real songs" in result["text"]
    assert "4. 换一批" in result["text"]
    assert "5. 随机匹配" in result["text"]
    assert "Do not call any tool yet" in result["text"]
    assert "qq_openid_raw" not in result["text"]


def test_smart_bgm_uses_jamendo_first_when_configured(cassette_env, monkeypatch):
    monkeypatch.setenv("JAMENDO_CLIENT_ID", "configured-client-id")
    monkeypatch.setattr(
        tools, "_safe_freetouse_category_summary", lambda: "- Travel (Video); related tags: sunny, uplifting"
    )
    tools._JAMENDO_DISABLED_CODE = None

    result = tools._rewrite_smart_bgm_keyword_selection(
        "jamendo-session",
        "安静、未来感、适合游戏菜单的背景音乐",
        2,
        optimization_enabled=False,
    )

    assert result["action"] == "rewrite"
    assert "Jamendo credentials appear configured" in result["text"]
    assert "jamendo_music_matcher" in result["text"]
    assert "Your next action must be a tool call" in result["text"]
    assert "searchTerms" in result["text"]
    assert "Do not generate or pass a raw Jamendo SearchPlan JSON" in result["text"]
    assert "Do not provide boost, order, type, duration" in result["text"]
    assert "Jamendo SearchPlan prompt" not in result["text"]
    assert "你是 Jamendo 音乐搜索策略生成器" not in result["text"]
    assert "Free To Use only as fallback" in result["text"]
    assert "Free To Use fallback category summary" in result["text"]
    assert "configured-client-id" not in result["text"]


def test_jamendo_api_error_disables_jamendo_bgm_fallback(cassette_env, monkeypatch):
    monkeypatch.setenv("JAMENDO_CLIENT_ID", "configured-client-id")
    tools._JAMENDO_DISABLED_CODE = None

    def fail_match(**kwargs):
        del kwargs
        raise tools.CassetteError("jamendo_api_error", "Jamendo returned invalid credentials")

    monkeypatch.setattr(tools.jamendo, "match_jamendo_music", fail_match)
    payload = json.loads(
        tools.jamendo_music_matcher(
            {
                "userQuery": "安静背景音乐",
                "searchPlan": {
                    "rawUserQuery": "安静背景音乐",
                    "audioFormat": "mp32",
                    "downloadFormat": "mp32",
                    "requireDownloadable": True,
                    "strategies": [{"name": "test", "fuzzyTags": ["ambient"], "limit": 1}],
                },
            }
        )
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "jamendo_api_error"
    assert tools._JAMENDO_DISABLED_CODE == "jamendo_api_error"

    monkeypatch.setattr(tools, "_safe_freetouse_category_summary", lambda: "- Chill (Genre); related tags: mellow")
    result = tools._rewrite_smart_bgm_keyword_selection(
        "jamendo-session",
        "安静背景音乐",
        1,
        optimization_enabled=False,
    )

    assert "jamendo_music_matcher" not in result["text"]
    assert "Choose exactly 3 Free To Use music search queries" in result["text"]
    tools._JAMENDO_DISABLED_CODE = None


def test_jamendo_non_auth_api_error_does_not_disable_provider(cassette_env, monkeypatch):
    monkeypatch.setenv("JAMENDO_CLIENT_ID", "configured-client-id")
    tools._JAMENDO_DISABLED_CODE = None

    def fail_match(**kwargs):
        del kwargs
        raise tools.CassetteError("jamendo_api_error", "Jamendo rejected a search parameter", {"status": "failed"})

    monkeypatch.setattr(tools.jamendo, "match_jamendo_music", fail_match)
    payload = json.loads(
        tools.jamendo_music_matcher(
            {
                "userQuery": "安静背景音乐",
                "searchPlan": {
                    "rawUserQuery": "安静背景音乐",
                    "audioFormat": "mp32",
                    "downloadFormat": "mp32",
                    "requireDownloadable": True,
                    "strategies": [{"name": "test", "fuzzyTags": ["ambient"], "limit": 1}],
                },
            }
        )
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "jamendo_api_error"
    assert tools._JAMENDO_DISABLED_CODE is None


def test_run_job_wait_true_persists_running_before_browser_finishes(cassette_env, monkeypatch):
    observed = {}

    def fake_browser_run(job):
        saved = jobs.load_job(job["job_id"])
        observed["status"] = saved["status"]
        observed["started_at"] = saved["started_at"]
        return {
            "status": "succeeded",
            "outputs": [],
            "questions": [],
            "errors": [],
            "quality": {},
            "final_screenshot": None,
        }

    monkeypatch.setattr(tools.browser, "run_cassette_browser_job", fake_browser_run)

    payload = json.loads(tools.cassette_run_job({"prompt": "Make a short edit", "session_id": "sync"}))

    assert payload["ok"] is True
    assert observed["status"] == "running"
    assert observed["started_at"]


def test_run_job_chat_message_only_uses_normal_job_path(cassette_env, monkeypatch):
    observed = {}

    def fake_browser_run(job):
        observed["prompt"] = job["prompt"]
        observed["chat_message"] = job["chat_message"]
        return {
            "status": "succeeded",
            "outputs": [],
            "questions": [],
            "errors": [],
            "quality": {},
            "final_screenshot": None,
        }

    monkeypatch.setattr(tools.browser, "run_cassette_browser_job", fake_browser_run)
    payload = json.loads(tools.cassette_run_job({"chat_message": "请剪成 10 秒", "session_id": "sync"}))

    assert payload["ok"] is True
    assert observed["prompt"] == "请剪成 10 秒"
    assert observed["chat_message"] == "请剪成 10 秒"


def test_gateway_run_job_forces_inprocess_background_to_keep_cut_responsive(cassette_env, monkeypatch):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    tools.cassette_ingest_media(
        {
            "source_path": str(media),
            "session_id": "gateway_media_telegram_chat_live",
            "platform": "telegram",
            "chat_id": "telegram_chat_raw",
            "user_id": "telegram_user_raw",
        }
    )
    observed = {}

    def fake_start(job):
        observed["job_id"] = job["job_id"]
        observed["delivery"] = dict(job.get("delivery") or {})
        observed["wait_path"] = "background"
        job["status"] = "running"
        job["started_at"] = jobs.now_iso()
        job["worker_kind"] = "thread"
        jobs.save_job(job)
        return job

    def fail_sync_browser(job):
        raise AssertionError("gateway jobs must not block in synchronous browser automation")

    monkeypatch.setattr(tools, "_start_inprocess_cassette_job", fake_start)
    monkeypatch.setattr(tools.browser, "run_cassette_browser_job", fail_sync_browser)

    payload = json.loads(
        tools.cassette_run_job(
            {
                "prompt": "internal",
                "chat_message": "Make a short edit",
                "session_id": "gateway_media_telegram_chat_live",
                "wait": True,
            }
        )
    )

    assert payload["ok"] is True
    assert observed["wait_path"] == "background"
    assert observed["delivery"]["platform"] == "telegram"
    assert payload["data"]["job"]["status"] == "running"
    assert payload["data"]["job"]["worker_kind"] == "thread"
    assert payload["data"]["background"] is True
    assert "Do not call cassette_job_status repeatedly" in payload["data"]["hermes_next_step"]


def test_running_gateway_background_status_discourages_polling(cassette_env):
    job = jobs.create_job(
        session_hash="background-status",
        prompt="internal",
        instruction="make a short edit",
        asset_paths=[],
        options={"cassette_session_id": "gateway_media_telegram_chat_status"},
    )
    job["status"] = "running"
    job["worker_kind"] = "thread"
    job["quality"] = {"gateway_background_job": True}
    jobs.save_job(job)

    payload = json.loads(tools.cassette_job_status({"job_id": job["job_id"]}))

    assert payload["ok"] is True
    assert payload["data"]["background"] is True
    assert "Do not call cassette_job_status repeatedly" in payload["data"]["hermes_next_step"]
    report = payload["data"]["job"]["report"]
    assert report["background"] is True
    assert report["next_check_after_sec"] >= 60
    assert "do not poll repeatedly" in report["user_summary"]


def test_completion_review_context_injected_for_hermes_supervisor(cassette_env):
    job = jobs.create_job(
        session_hash="review-context",
        prompt="internal",
        instruction="make a short edit",
        asset_paths=[],
        options={"cassette_session_id": "gateway_media_telegram_review"},
    )
    job["status"] = "needs_user"
    job["questions"] = [
        {
            "question": "Cassette says the timeline is ready and the export button is enabled.",
            "requires_user": False,
            "reason": "completion_requires_hermes_review",
            "answer": "Hermes supervisor semantic review is required before export.",
        }
    ]
    jobs.save_job(job)

    context = tools.inject_cassette_context()

    assert context is not None
    assert "Cassette completion review required" in context
    assert job["job_id"] in context
    assert "cassette_review_completion" in context
    assert 'decision="export"' in context


def test_completion_review_export_uses_browser_session(cassette_env, monkeypatch):
    job = jobs.create_job(
        session_hash="review-export",
        prompt="internal",
        instruction="make a short edit",
        asset_paths=[],
        options={"cassette_session_id": "gateway_media_telegram_review_export"},
    )
    job["status"] = "needs_user"
    job["questions"] = [{"reason": "completion_requires_hermes_review", "question": "ready"}]
    jobs.save_job(job)
    observed = {}

    def fake_export(saved_job, decision):
        observed["job_id"] = saved_job["job_id"]
        observed["decision"] = decision
        return {
            "status": "succeeded",
            "outputs": [{"download": "out.mp4", "local_path": str(cassette_env["asset_root"] / "exports" / "out.mp4")}],
            "questions": saved_job.get("questions") or [],
            "errors": [],
            "quality": {"completion_source": "hermes_completion_review", "completion_review": decision},
            "final_screenshot": None,
        }

    monkeypatch.setattr(tools.browser, "export_reviewed_completion_job_threaded", fake_export)
    monkeypatch.setattr(tools.notifier, "notify_terminal_job", lambda job: {"status": "skipped"})

    payload = json.loads(
        tools.cassette_review_completion(
            {
                "job_id": job["job_id"],
                "decision": "export",
                "reason": "Cassette says the edit is ready enough to export.",
            }
        )
    )

    assert payload["ok"] is True
    assert observed["job_id"] == job["job_id"]
    assert observed["decision"]["decision"] == "export"
    saved = jobs.load_job(job["job_id"])
    assert saved["status"] == "succeeded"
    assert saved["quality"]["completion_source"] == "hermes_completion_review"


def test_run_job_browser_automation_uses_dedicated_thread(cassette_env, monkeypatch):
    observed_thread_ids = []

    def fake_browser_run(job):
        observed_thread_ids.append(threading.get_ident())
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            observed_no_loop = True
        else:
            observed_no_loop = False
        return {
            "status": "succeeded",
            "outputs": [],
            "questions": [],
            "errors": [],
            "quality": {"observed_no_loop": observed_no_loop},
            "final_screenshot": None,
        }

    monkeypatch.setattr(tools.browser, "run_cassette_browser_job", fake_browser_run)

    async def run_inside_event_loop():
        return json.loads(tools.cassette_run_job({"prompt": "internal", "session_id": "async-loop"}))

    with ThreadPoolExecutor(max_workers=1) as executor:
        payload = executor.submit(lambda: asyncio.run(run_inside_event_loop())).result()

    assert payload["ok"] is True
    assert len(set(observed_thread_ids)) == 1
    assert payload["data"]["job"]["quality"]["observed_no_loop"] is True


def test_run_job_reuses_same_browser_worker_across_caller_threads(cassette_env, monkeypatch):
    observed_thread_ids = []

    def fake_browser_run(job):
        observed_thread_ids.append(threading.get_ident())
        return {
            "status": "succeeded",
            "outputs": [],
            "questions": [],
            "errors": [],
            "quality": {},
            "final_screenshot": None,
        }

    monkeypatch.setattr(tools.browser, "run_cassette_browser_job", fake_browser_run)

    def call_tool(index):
        return json.loads(
            tools.cassette_run_job({"prompt": f"internal {index}", "session_id": f"threaded-reuse-{index}"})
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(call_tool, [1, 2]))

    assert all(result["ok"] for result in results)
    assert len(observed_thread_ids) == 2
    assert len(set(observed_thread_ids)) == 1


def test_run_job_rejects_second_active_job_for_same_session(cassette_env):
    session_id = "single-session"
    session_hash = tools.manifest.resolve_session_hash(session_id=session_id)
    job = jobs.create_job(
        session_hash,
        "internal",
        "instruction",
        [],
        {"cassette_session_id": session_id, "delivery": {"platform": "web", "chat_id": session_id}},
    )
    job["status"] = "running"
    jobs.save_job(job)

    payload = json.loads(tools.cassette_run_job({"prompt": "second", "session_id": session_id}))

    assert payload["ok"] is False
    assert payload["error"]["code"] == "cassette_job_already_running"
    assert payload["error"]["message"] == "请使用/cut命令终止当前流程或剪辑任务后再尝试开始新的剪辑任务"


def test_run_job_defaults_to_thirty_minute_timeout(cassette_env, monkeypatch):
    observed = {}

    def fake_browser_run(job):
        observed["timeout_sec"] = job["timeout_sec"]
        return {
            "status": "succeeded",
            "outputs": [],
            "questions": [],
            "errors": [],
            "quality": {},
            "final_screenshot": None,
        }

    monkeypatch.delenv("CASSETTE_BROWSER_TIMEOUT_SEC", raising=False)
    monkeypatch.setattr(tools.browser, "run_cassette_browser_job", fake_browser_run)
    payload = json.loads(tools.cassette_run_job({"prompt": "Make a short edit", "session_id": "default-timeout"}))

    assert payload["ok"] is True
    assert observed["timeout_sec"] == 1800


def test_run_job_clamps_short_timeout_to_runtime_minimum(cassette_env, monkeypatch):
    monkeypatch.setenv("CASSETTE_MIN_BROWSER_TIMEOUT_SEC", "1200")
    observed = {}

    def fake_browser_run(job):
        observed["timeout_sec"] = job["timeout_sec"]
        return {
            "status": "succeeded",
            "outputs": [],
            "questions": [],
            "errors": [],
            "quality": {},
            "final_screenshot": None,
        }

    monkeypatch.setattr(tools.browser, "run_cassette_browser_job", fake_browser_run)
    payload = json.loads(
        tools.cassette_run_job({"prompt": "Make a short edit", "session_id": "sync", "timeout_sec": 600})
    )

    assert payload["ok"] is True
    assert observed["timeout_sec"] == 1200


def test_run_job_preserves_browser_progress_events(cassette_env, monkeypatch):
    def fake_browser_run(job):
        jobs.update_job(
            job["job_id"],
            progress_events=[
                {
                    "at": "2026-05-12T04:36:58Z",
                    "status": "running",
                    "summary": "Export status: Rendering on AWS Lambda",
                    "output_link_count": 0,
                }
            ],
        )
        return {
            "status": "failed",
            "outputs": [],
            "questions": [],
            "errors": [{"code": "export_timeout", "message": "Timed out waiting for Cassette export download."}],
            "quality": {"completion_observed": True, "export_completed": False},
            "final_screenshot": None,
        }

    monkeypatch.setattr(tools.browser, "run_cassette_browser_job", fake_browser_run)
    payload = json.loads(tools.cassette_run_job({"prompt": "Make a short edit", "session_id": "sync"}))
    job = jobs.load_job(payload["job_id"])

    assert payload["ok"] is True
    assert job["status"] == "failed"
    assert job["progress_events"][0]["summary"] == "Export status: Rendering on AWS Lambda"


def test_job_status_includes_user_report(cassette_env, monkeypatch):
    def fake_browser_run(job):
        return {
            "status": "succeeded",
            "outputs": [],
            "questions": [],
            "errors": [],
            "quality": {"export_pending": True, "progress_summary": "Cassette chat says complete."},
            "final_screenshot": None,
        }

    monkeypatch.setattr(tools.browser, "run_cassette_browser_job", fake_browser_run)
    payload = json.loads(
        tools.cassette_run_job({"prompt": "internal", "chat_message": "请剪成 10 秒", "session_id": "report"})
    )
    status = json.loads(tools.cassette_job_status({"job_id": payload["job_id"]}))

    report = status["data"]["job"]["report"]
    assert report["status"] == "succeeded"
    assert report["export_pending"] is True
    assert "no exported video was recorded" in report["user_summary"]
    assert report["latest_progress"] == "Cassette chat says complete."


def test_run_job_does_not_hardcode_default_model(cassette_env, monkeypatch):
    observed = {}

    def fake_browser_run(job):
        observed["model_selection"] = job["model_selection"]
        return {
            "status": "succeeded",
            "outputs": [],
            "questions": [],
            "errors": [],
            "quality": {},
            "final_screenshot": None,
        }

    monkeypatch.setattr(tools.browser, "run_cassette_browser_job", fake_browser_run)
    payload = json.loads(
        tools.cassette_run_job({"prompt": "internal", "chat_message": "请剪成 10 秒", "session_id": "model-default"})
    )
    status = json.loads(tools.cassette_job_status({"job_id": payload["job_id"]}))

    assert observed["model_selection"]["model"] == ""
    assert observed["model_selection"]["thinking_level"] == "Low"
    assert status["data"]["job"]["report"]["model_selection"]["model"] == ""


def test_run_job_accepts_user_specified_cassette_model(cassette_env, monkeypatch):
    observed = {}

    def fake_browser_run(job):
        observed["model_selection"] = job["model_selection"]
        return {
            "status": "succeeded",
            "outputs": [],
            "questions": [],
            "errors": [],
            "quality": {},
            "final_screenshot": None,
        }

    monkeypatch.setattr(tools.browser, "run_cassette_browser_job", fake_browser_run)
    json.loads(
        tools.cassette_run_job(
            {
                "prompt": "internal",
                "chat_message": "请用 DeepSeek V4 Pro，高思考程度，剪成 10 秒",
                "cassette_model": "DeepSeek V4 Pro",
                "session_id": "model-explicit",
            }
        )
    )

    assert observed["model_selection"]["model"] == "DeepSeek V4 Pro"
    assert observed["model_selection"]["thinking_level"] == "High"


def test_run_job_does_not_treat_editing_words_as_thinking_level(cassette_env, monkeypatch):
    observed = {}

    def fake_browser_run(job):
        observed["model_selection"] = job["model_selection"]
        return {
            "status": "succeeded",
            "outputs": [],
            "questions": [],
            "errors": [],
            "quality": {},
            "final_screenshot": None,
        }

    monkeypatch.setattr(tools.browser, "run_cassette_browser_job", fake_browser_run)
    json.loads(
        tools.cassette_run_job(
            {
                "prompt": "internal",
                "chat_message": "把高光镜头放中后段，节奏轻快",
                "session_id": "model-edit-words",
            }
        )
    )

    assert observed["model_selection"]["thinking_level"] == "Low"


def test_gateway_run_job_uses_session_model_preference_over_prompt_text(cassette_env, monkeypatch):
    observed = {}
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    source = SimpleNamespace(
        platform=SimpleNamespace(value="qqbot"), chat_id="qq_openid_raw", user_id="qq_user_raw", chat_type="dm"
    )
    tools.ingest_gateway_media(
        event=SimpleNamespace(
            source=source, media_urls=[str(media)], media_types=["video/mp4"], text="", message_id="raw_message_id"
        ),
        gateway=SimpleNamespace(_is_user_authorized=lambda _: True),
    )
    session_id = tools._gateway_session_id(SimpleNamespace(source=source), None)
    tools._save_cassette_model_preference(session_id, "Kimi K2.6", "Medium", source="test")

    def fake_browser_run(job):
        observed["model_selection"] = job["model_selection"]
        return {
            "status": "succeeded",
            "outputs": [],
            "questions": [],
            "errors": [],
            "quality": {},
            "final_screenshot": None,
        }

    monkeypatch.setenv("CASSETTE_GATEWAY_BACKGROUND_JOBS", "false")
    monkeypatch.setattr(tools.browser, "run_cassette_browser_job", fake_browser_run)
    json.loads(
        tools.cassette_run_job(
            {
                "prompt": "internal",
                "chat_message": "请用 DeepSeek V4 Flash，高思考程度，剪成 10 秒",
                "session_id": session_id,
                "wait": True,
            }
        )
    )

    assert observed["model_selection"]["model"] == "Kimi K2.6"
    assert observed["model_selection"]["thinking_level"] == "Medium"
    assert observed["model_selection"]["source"] == "session_preference"


def test_job_status_scrubs_raw_delivery_target(cassette_env, monkeypatch):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    tools.cassette_ingest_media(
        {
            "source_path": str(media),
            "chat_id": "wxid_chat_raw",
            "user_id": "wxid_user_raw",
            "platform": "weixin",
            "session_id": "delivery",
        }
    )

    def fake_browser_run(job):
        assert job["delivery"]["chat_id"] == "wxid_chat_raw"
        return {
            "status": "succeeded",
            "outputs": [],
            "questions": [],
            "errors": [],
            "quality": {},
            "final_screenshot": None,
        }

    monkeypatch.setattr(tools.browser, "run_cassette_browser_job", fake_browser_run)
    payload = json.loads(
        tools.cassette_run_job({"prompt": "internal", "chat_message": "请剪成 10 秒", "session_id": "delivery"})
    )
    status = json.loads(tools.cassette_job_status({"job_id": payload["job_id"]}))
    serialized = json.dumps(status, ensure_ascii=False)

    assert "wxid_chat_raw" not in serialized
    assert "wxid_user_raw" not in serialized
    assert status["data"]["job"]["delivery"]["platform"] == "weixin"
    assert status["data"]["job"]["delivery"]["has_raw_target"] is True


def test_run_job_accepts_manifest_session_hash_from_list_assets(cassette_env, monkeypatch):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    tools.cassette_ingest_media({"source_path": str(media), "session_id": "gateway_media_weixin_test"})
    listed = json.loads(tools.cassette_list_assets({"session_id": "gateway_media_weixin_test"}))
    manifest_hash = listed["data"]["manifest"]["session_hash"]
    observed = {}

    def fake_browser_run(job):
        observed["asset_paths"] = job["asset_paths"]
        return {
            "status": "needs_user",
            "outputs": [],
            "questions": [],
            "errors": [],
            "quality": {},
            "final_screenshot": None,
        }

    monkeypatch.setattr(tools.browser, "run_cassette_browser_job", fake_browser_run)
    payload = json.loads(tools.cassette_run_job({"prompt": "internal", "session_id": manifest_hash}))

    assert payload["ok"] is True
    assert len(observed["asset_paths"]) == 1


def test_ingest_gateway_media_skips_unauthorized_video(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    source = SimpleNamespace(
        platform=SimpleNamespace(value="weixin"),
        chat_id="wxid_chat_raw",
        user_id="wxid_user_raw",
    )
    event = SimpleNamespace(
        source=source,
        media_urls=[str(media)],
        media_types=["video/mp4"],
        text="",
        message_id="raw_message_id",
    )
    gateway = SimpleNamespace(_is_user_authorized=lambda _: False)

    assert tools.ingest_gateway_media(event=event, gateway=gateway) is None
    assert not cassette_env["asset_root"].exists()
