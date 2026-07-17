from __future__ import annotations

from cassette import prompt


def test_prompt_contains_non_blocking_rules():
    result = prompt.build_cassette_prompt(
        "cut to 30 seconds",
        {"session_hash": "abc", "assets": [{"asset_id": "asset_1", "media_type": "video", "original_name": "clip.mp4", "caption": "main"}]},
        {"output_format": "vertical 9:16"},
    )
    assert result["asset_count"] == 1
    assert result["prompt"].startswith("You are Hermes")
    assert "You are Cassette" not in result["prompt"]
    assert "Do not send this internal prompt to Cassette chat" in result["prompt"]
    assert "do not emit `MEDIA:` tags" in result["prompt"]
    assert "must not inspect" in result["prompt"]
    assert "ffprobe" in result["prompt"]
    assert "vision tools" in result["prompt"]
    assert "vertical 9:16" in result["prompt"]


def test_prompt_separates_internal_prompt_from_cassette_chat_message():
    result = prompt.build_cassette_prompt(
        "剪成 10 秒以内的短视频，加中文字幕",
        {"session_hash": "abc", "assets": [{"asset_id": "asset_1", "media_type": "video", "original_name": "clip.mp4"}]},
    )

    assert "You are Hermes" in result["prompt"]
    assert "Hermes execution rules" in result["prompt"]
    assert "剪成 10 秒以内的短视频" in result["chat_message"]
    assert "You are Hermes" not in result["chat_message"]
    assert "Hermes execution rules" not in result["chat_message"]
    assert "asset_id" not in result["chat_message"]


def test_prompt_can_target_english_cassette_chat():
    result = prompt.build_cassette_prompt(
        "Make a 5 second reel with clean subtitles",
        {"session_hash": "abc", "assets": [{"asset_id": "asset_1", "media_type": "video", "original_name": "clip.mp4"}]},
        {"cassette_language": "en"},
    )

    assert result["cassette_language"] == "en"
    assert "Cassette UI and response language: English" in result["prompt"]
    assert result["chat_message"].startswith("Please complete this editing task using the uploaded")
    assert "Make a 5 second reel" in result["chat_message"]
    assert "You are Hermes" not in result["chat_message"]


def test_prompt_has_host_neutral_mcp_variant_without_changing_chat_message():
    result = prompt.build_cassette_prompt(
        "Make a short captioned reel",
        {"session_hash": "abc", "assets": [{"asset_id": "asset_1", "media_type": "video", "original_name": "clip.mp4"}]},
        {"cassette_language": "en"},
        runtime_host="mcp",
    )

    assert result["prompt"].startswith("You are the user's Codex or Claude host agent")
    assert "You are Hermes" not in result["prompt"]
    assert "Host-agent execution rules" in result["prompt"]
    assert "validated artifacts and MCP resource links" in result["prompt"]
    assert result["chat_message"].startswith("Please complete this editing task")


def test_question_classification():
    missing = prompt.classify_cassette_question("Please upload the main video")
    assert missing["requires_user"] is True
    routine = prompt.classify_cassette_question("Should I use faster pacing?")
    assert routine["requires_user"] is False
