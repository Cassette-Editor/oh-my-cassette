from __future__ import annotations

import sys
import types
from pathlib import Path

from cassette import notifier


def test_weixin_final_message_contains_status_without_delivery_target():
    message = notifier.format_platform_final_message(
        {
            "job_id": "cassette_test",
            "status": "failed",
            "errors": [{"code": "asset_upload_failed"}],
            "delivery": {"chat_id": "wxid_chat_raw", "user_id": "wxid_user_raw"},
            "quality": {"progress_summary": "Cassette upload status reported 0 ready, 1 failed."},
        },
        platform="weixin",
    )

    assert "asset_upload_failed" in message
    assert "Cassette upload status" in message
    assert "wxid_chat_raw" not in message
    assert "wxid_user_raw" not in message


def test_weixin_final_message_mentions_export_without_local_path(tmp_path: Path):
    exported = tmp_path / "result.mp4"
    exported.write_bytes(b"video")

    message = notifier.format_platform_final_message(
        {
            "job_id": "cassette_test",
            "status": "succeeded",
            "outputs": [{"local_path": str(exported), "download": exported.name}],
            "quality": {"progress_summary": "剪辑完成，可以导出。"},
        },
        platform="weixin",
    )

    assert "导出视频已生成" in message
    assert str(exported) not in message


def test_weixin_final_message_mentions_compatible_mp4_without_local_path(tmp_path: Path):
    exported = tmp_path / "result.mp4"
    exported.write_bytes(b"video")

    message = notifier.format_platform_final_message(
        {
            "job_id": "cassette_test",
            "status": "succeeded",
            "outputs": [{"local_path": str(exported), "download": exported.name}],
        },
        media_delivery="sent_compatible",
        platform="weixin",
    )

    assert "微信兼容 MP4" in message
    assert "zip" not in message.lower()
    assert str(exported) not in message


def test_weixin_final_message_explains_preview_and_original_zip(tmp_path: Path):
    exported = tmp_path / "result.mp4"
    exported.write_bytes(b"video")

    message = notifier.format_platform_final_message(
        {
            "job_id": "cassette_test",
            "status": "succeeded",
            "outputs": [{"local_path": str(exported), "download": exported.name}],
        },
        media_delivery="sent_preview_zip",
        platform="weixin",
    )

    assert "低码率 MP4 预览" in message
    assert "zip 文件包含原始大小导出视频" in message
    assert str(exported) not in message


def test_openclaw_aes_key_conversion_from_hermes_hex_base64():
    raw_key = bytes(range(16))
    hermes_shape = notifier.base64.b64encode(raw_key.hex().encode("ascii")).decode("ascii")

    converted = notifier._openclaw_aes_key_for_api(hermes_shape)

    assert converted == notifier.base64.b64encode(raw_key).decode("ascii")


def test_openclaw_aes_key_conversion_leaves_unknown_shape_unchanged():
    value = notifier.base64.b64encode(b"not-a-hex-key").decode("ascii")

    assert notifier._openclaw_aes_key_for_api(value) == value


def test_runtime_env_reads_hermes_dotenv_without_exporting(tmp_path: Path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / ".env").write_text(
        "QQ_APP_ID=dotenv-app\nexport QQ_CLIENT_SECRET='dotenv-secret'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("QQ_APP_ID", raising=False)
    monkeypatch.delenv("QQ_CLIENT_SECRET", raising=False)

    assert notifier._runtime_env("QQ_APP_ID") == "dotenv-app"
    assert notifier._runtime_env("QQ_CLIENT_SECRET") == "dotenv-secret"
    assert notifier.os.getenv("QQ_APP_ID") is None


def test_zip_original_export_contains_original_mp4(tmp_path: Path):
    exported = tmp_path / "result.mp4"
    exported.write_bytes(b"video")

    archive = notifier._zip_original_export(str(exported))

    assert archive.name == "result.original.zip"
    assert archive.parent.name == "weixin_delivery"
    assert archive.exists()


def test_prepare_weixin_video_delivery_file_uses_non_faststart_mp4(tmp_path: Path, monkeypatch):
    source = tmp_path / "result.mp4"
    source.write_bytes(b"video")
    captured: dict = {}

    def fake_run(cmd, stdout, stderr, text):
        captured["cmd"] = cmd
        Path(cmd[-1]).write_bytes(b"weixin-video")
        return type("Proc", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(notifier.subprocess, "run", fake_run)

    delivered = notifier._prepare_weixin_video_delivery_file(str(source))

    assert delivered.name == "result.weixin-480p.mp4"
    assert delivered.parent.name == "weixin_delivery"
    assert delivered.read_bytes() == b"weixin-video"
    assert "+faststart" not in captured["cmd"]


def test_notify_terminal_job_reports_weixin_video_send_failure(tmp_path: Path, monkeypatch):
    exported = tmp_path / "result.mp4"
    exported.write_bytes(b"video")
    sent_texts: list[str] = []

    async def fake_send_video(chat_id: str, file_path: str) -> dict:
        assert file_path == str(exported)
        return {"success": False, "error": "CDN upload HTTP 500"}

    async def fake_send_text(chat_id: str, message: str) -> dict:
        sent_texts.append(message)
        return {"success": True, "message_id": "text-ok"}

    monkeypatch.setenv("WEIXIN_TOKEN", "token")
    monkeypatch.setenv("WEIXIN_ACCOUNT_ID", "account")
    monkeypatch.setattr(notifier, "_send_weixin_video_attachment", fake_send_video)
    monkeypatch.setattr(notifier, "_send_weixin_text", fake_send_text)

    result = notifier.notify_terminal_job(
        {
            "job_id": "cassette_test",
            "status": "succeeded",
            "outputs": [{"local_path": str(exported), "download": exported.name}],
            "delivery": {"platform": "weixin", "chat_id": "wxid_chat_raw"},
            "quality": {"progress_summary": "剪辑完成，可以导出。"},
        }
    )

    assert result["status"] == "partial"
    assert result["code"] == "weixin_video_send_failed"
    assert result["message_id"] == "text-ok"
    assert "视频发送失败" in sent_texts[0]
    assert "MEDIA:" not in sent_texts[0]
    assert str(exported) not in sent_texts[0]


def test_notify_terminal_job_sends_weixin_export_as_video(tmp_path: Path, monkeypatch):
    exported = tmp_path / "result.mp4"
    exported.write_bytes(b"video")
    calls: list[tuple[str, str]] = []

    async def fake_send_video(chat_id: str, file_path: str) -> dict:
        calls.append(("video", file_path))
        return {"success": True, "message_id": "media-ok", "mode": "native"}

    async def fake_send_text(chat_id: str, message: str) -> dict:
        calls.append(("text", message))
        return {"success": True, "message_id": "text-ok"}

    monkeypatch.setenv("WEIXIN_TOKEN", "token")
    monkeypatch.setenv("WEIXIN_ACCOUNT_ID", "account")
    monkeypatch.setattr(notifier, "_send_weixin_video_attachment", fake_send_video)
    monkeypatch.setattr(notifier, "_send_weixin_text", fake_send_text)

    result = notifier.notify_terminal_job(
        {
            "job_id": "cassette_test",
            "status": "succeeded",
            "outputs": [{"local_path": str(exported), "download": exported.name}],
            "delivery": {"platform": "weixin", "chat_id": "wxid_chat_raw"},
            "quality": {"progress_summary": "剪辑完成，可以导出。"},
        }
    )

    assert result["status"] == "sent"
    assert result["message_id"] == "text-ok"
    assert result["media_message_id"] == "media-ok"
    assert result["media_mode"] == "native"
    assert calls[0] == ("video", str(exported))
    assert calls[1][0] == "text"
    assert "MEDIA:" not in calls[1][1]


def test_notify_terminal_job_reports_weixin_compatible_video_mode(tmp_path: Path, monkeypatch):
    exported = tmp_path / "result.mp4"
    exported.write_bytes(b"video")
    sent_texts: list[str] = []
    file_calls: list[str] = []

    async def fake_send_video(chat_id: str, file_path: str) -> dict:
        return {"success": True, "message_id": "media-ok", "mode": "weixin_compatible_mp4"}

    async def fake_send_file(chat_id: str, file_path: str) -> dict:
        file_calls.append(file_path)
        return {"success": True, "message_id": "zip-ok"}

    async def fake_send_text(chat_id: str, message: str) -> dict:
        sent_texts.append(message)
        return {"success": True, "message_id": "text-ok"}

    monkeypatch.setenv("WEIXIN_TOKEN", "token")
    monkeypatch.setenv("WEIXIN_ACCOUNT_ID", "account")
    monkeypatch.setenv("CASSETTE_WEIXIN_SEND_ORIGINAL_ZIP", "1")
    monkeypatch.setattr(notifier, "_send_weixin_video_attachment", fake_send_video)
    monkeypatch.setattr(notifier, "_send_weixin_file_attachment", fake_send_file)
    monkeypatch.setattr(notifier, "_send_weixin_text", fake_send_text)

    result = notifier.notify_terminal_job(
        {
            "job_id": "cassette_test",
            "status": "succeeded",
            "outputs": [{"local_path": str(exported), "download": exported.name}],
            "delivery": {"platform": "weixin", "chat_id": "wxid_chat_raw"},
        }
    )

    assert result["status"] == "sent"
    assert result["media_mode"] == "weixin_compatible_mp4"
    assert result["zip_message_id"] == "zip-ok"
    assert file_calls[0].endswith(".original.zip")
    assert "低码率 MP4 预览" in sent_texts[0]
    assert "zip 文件" in sent_texts[0]


def test_notify_terminal_job_does_not_send_original_zip_by_default(tmp_path: Path, monkeypatch):
    exported = tmp_path / "result.mp4"
    exported.write_bytes(b"video")
    sent_texts: list[str] = []
    file_calls: list[str] = []

    async def fake_send_video(chat_id: str, file_path: str) -> dict:
        return {"success": True, "message_id": "media-ok", "mode": "weixin_compatible_mp4"}

    async def fake_send_file(chat_id: str, file_path: str) -> dict:
        file_calls.append(file_path)
        return {"success": True, "message_id": "zip-ok"}

    async def fake_send_text(chat_id: str, message: str) -> dict:
        sent_texts.append(message)
        return {"success": True, "message_id": "text-ok"}

    monkeypatch.setenv("WEIXIN_TOKEN", "token")
    monkeypatch.setenv("WEIXIN_ACCOUNT_ID", "account")
    monkeypatch.delenv("CASSETTE_WEIXIN_SEND_ORIGINAL_ZIP", raising=False)
    monkeypatch.setattr(notifier, "_send_weixin_video_attachment", fake_send_video)
    monkeypatch.setattr(notifier, "_send_weixin_file_attachment", fake_send_file)
    monkeypatch.setattr(notifier, "_send_weixin_text", fake_send_text)

    result = notifier.notify_terminal_job(
        {
            "job_id": "cassette_test",
            "status": "succeeded",
            "outputs": [{"local_path": str(exported), "download": exported.name}],
            "delivery": {"platform": "weixin", "chat_id": "wxid_chat_raw"},
        }
    )

    assert result["status"] == "sent"
    assert result["media_mode"] == "weixin_compatible_mp4"
    assert "zip_message_id" not in result
    assert file_calls == []
    assert "微信兼容 MP4 预览" in sent_texts[0]
    assert "zip" not in sent_texts[0].lower()


def test_notify_terminal_job_reports_preview_when_original_zip_fails(tmp_path: Path, monkeypatch):
    exported = tmp_path / "result.mp4"
    exported.write_bytes(b"video")
    sent_texts: list[str] = []

    async def fake_send_video(chat_id: str, file_path: str) -> dict:
        return {"success": True, "message_id": "media-ok", "mode": "weixin_compatible_mp4"}

    async def fake_send_file(chat_id: str, file_path: str) -> dict:
        return {"success": False, "error": "file upload failed"}

    async def fake_send_text(chat_id: str, message: str) -> dict:
        sent_texts.append(message)
        return {"success": True, "message_id": "text-ok"}

    monkeypatch.setenv("WEIXIN_TOKEN", "token")
    monkeypatch.setenv("WEIXIN_ACCOUNT_ID", "account")
    monkeypatch.setenv("CASSETTE_WEIXIN_SEND_ORIGINAL_ZIP", "1")
    monkeypatch.setattr(notifier, "_send_weixin_video_attachment", fake_send_video)
    monkeypatch.setattr(notifier, "_send_weixin_file_attachment", fake_send_file)
    monkeypatch.setattr(notifier, "_send_weixin_text", fake_send_text)

    result = notifier.notify_terminal_job(
        {
            "job_id": "cassette_test",
            "status": "succeeded",
            "outputs": [{"local_path": str(exported), "download": exported.name}],
            "delivery": {"platform": "weixin", "chat_id": "wxid_chat_raw"},
        }
    )

    assert result["status"] == "sent"
    assert result["zip_error"] == "file upload failed"
    assert "zip 文件发送失败" in sent_texts[0]


def test_notify_terminal_job_sends_qq_export_as_video(tmp_path: Path, monkeypatch):
    exported = tmp_path / "result.mp4"
    exported.write_bytes(b"video")
    calls: list[tuple[str, str, str]] = []

    async def fake_send_video(chat_id: str, file_path: str, chat_type: str = "") -> dict:
        calls.append(("video", file_path, chat_type))
        return {"success": True, "message_id": "qq-media-ok", "mode": "native"}

    async def fake_send_text(chat_id: str, message: str, chat_type: str = "") -> dict:
        calls.append(("text", message, chat_type))
        return {"success": True, "message_id": "qq-text-ok"}

    monkeypatch.setenv("QQ_APP_ID", "app")
    monkeypatch.setenv("QQ_CLIENT_SECRET", "secret")
    monkeypatch.setattr(notifier, "_send_qq_video_attachment", fake_send_video)
    monkeypatch.setattr(notifier, "_send_qq_text", fake_send_text)

    result = notifier.notify_terminal_job(
        {
            "job_id": "cassette_test",
            "status": "succeeded",
            "outputs": [{"local_path": str(exported), "download": exported.name}],
            "delivery": {"platform": "qqbot", "chat_id": "qq_openid_raw", "chat_type": "dm"},
            "quality": {"progress_summary": "剪辑完成，可以导出。"},
        }
    )

    assert result["status"] == "sent"
    assert result["platform"] == "qqbot"
    assert result["message_id"] == "qq-text-ok"
    assert result["media_message_id"] == "qq-media-ok"
    assert result["media_mode"] == "native"
    assert calls[0] == ("video", str(exported), "dm")
    assert calls[1][0] == "text"
    assert "MEDIA:" not in calls[1][1]
    assert str(exported) not in calls[1][1]


def test_notify_terminal_job_sends_telegram_export_as_video_with_english_message(tmp_path: Path, monkeypatch):
    exported = tmp_path / "result.mp4"
    exported.write_bytes(b"video")
    calls: list[tuple[str, str, str | None]] = []

    async def fake_send_video(chat_id: str, file_path: str, thread_id: str | None = None) -> dict:
        calls.append(("video", file_path, thread_id))
        return {"success": True, "message_id": "tg-media-ok", "mode": "native"}

    async def fake_send_text(chat_id: str, message: str, thread_id: str | None = None) -> dict:
        calls.append(("text", message, thread_id))
        return {"success": True, "message_id": "tg-text-ok"}

    monkeypatch.setattr(notifier, "_send_telegram_video_attachment", fake_send_video)
    monkeypatch.setattr(notifier, "_send_telegram_text", fake_send_text)

    result = notifier.notify_terminal_job(
        {
            "job_id": "cassette_test",
            "status": "succeeded",
            "cassette_language": "en",
            "outputs": [{"local_path": str(exported), "download": exported.name}],
            "delivery": {"platform": "telegram", "chat_id": "telegram_chat_raw", "thread_id": "topic_raw"},
            "quality": {"progress_summary": "The edit is complete and ready to export."},
        }
    )

    assert result["status"] == "sent"
    assert result["platform"] == "telegram"
    assert result["message_id"] == "tg-text-ok"
    assert result["media_message_id"] == "tg-media-ok"
    assert calls[0] == ("video", str(exported), "topic_raw")
    assert calls[1][0] == "text"
    assert calls[1][2] == "topic_raw"
    assert "Exported video has been sent" in calls[1][1]
    assert str(exported) not in calls[1][1]
    assert "telegram_chat_raw" not in calls[1][1]


def test_terminal_final_message_prefers_edit_summary_over_export_status(tmp_path: Path):
    exported = tmp_path / "result.mp4"
    exported.write_bytes(b"video")

    message = notifier.format_platform_final_message(
        {
            "job_id": "cassette_test",
            "status": "succeeded",
            "cassette_language": "en",
            "outputs": [{"local_path": str(exported), "download": exported.name}],
            "quality": {
                "progress_summary": "The cat video now uses Sunroof as BGM, with original audio lowered and the song climax aligned to the video. Copy",
            },
            "progress_events": [
                {"summary": "Cassette upload status: 2 ready, 0 failed."},
                {"summary": "Export status: Render complete"},
            ],
        },
        media_delivery="sent",
        platform="telegram",
    )

    assert "**Edit Summary**" in message
    assert "- The cat video now uses Sunroof" in message
    assert "Export status: Render complete" not in message
    assert not message.endswith("Copy")
    assert "**Delivery**" in message


def test_telegram_final_message_explains_preview_for_large_export(tmp_path: Path):
    exported = tmp_path / "exports" / "cassette_test" / "result.mp4"
    exported.parent.mkdir(parents=True)
    exported.write_bytes(b"video")

    message = notifier.format_platform_final_message(
        {
            "job_id": "cassette_test",
            "status": "succeeded",
            "cassette_language": "en",
            "outputs": [{"local_path": str(exported), "download": exported.name}],
            "quality": {"progress_summary": "Travel vlog assembled with chill BGM and subtitles."},
        },
        media_delivery="sent_telegram_preview",
        platform="telegram",
    )

    assert "Telegram Bot API limits uploaded videos to 50 MB" in message
    assert "compressed preview video" in message
    assert "${CASSETTE_ASSET_ROOT}/exports/cassette_test/result.mp4" in message
    assert str(tmp_path) not in message


def test_notify_terminal_job_sends_telegram_preview_when_export_exceeds_limit(tmp_path: Path, monkeypatch):
    exported = tmp_path / "result.mp4"
    exported.write_bytes(b"x" * 120)
    preview = tmp_path / "telegram-preview.mp4"
    preview.write_bytes(b"preview")
    calls: list[tuple[str, str, str | None]] = []
    sent_texts: list[str] = []

    async def fake_send_video(chat_id: str, file_path: str, thread_id: str | None = None) -> dict:
        calls.append((chat_id, file_path, thread_id))
        return {"success": True, "message_id": "tg-preview-ok", "mode": "native"}

    async def fake_send_text(chat_id: str, message: str, thread_id: str | None = None) -> dict:
        sent_texts.append(message)
        return {"success": True, "message_id": "tg-text-ok"}

    monkeypatch.setenv("CASSETTE_TELEGRAM_VIDEO_MAX_BYTES", "100")
    monkeypatch.setattr(notifier, "_prepare_telegram_preview_video", lambda path: preview)
    monkeypatch.setattr(notifier, "_send_telegram_video_attachment", fake_send_video)
    monkeypatch.setattr(notifier, "_send_telegram_text", fake_send_text)

    result = notifier.notify_terminal_job(
        {
            "job_id": "cassette_test",
            "status": "succeeded",
            "cassette_language": "en",
            "outputs": [{"local_path": str(exported), "download": exported.name}],
            "delivery": {"platform": "telegram", "chat_id": "telegram_chat_raw", "thread_id": "123"},
            "quality": {"progress_summary": "Travel vlog assembled with chill BGM and subtitles."},
        }
    )

    assert result["status"] == "sent"
    assert result["original_too_large"] is True
    assert result["media_message_id"] == "tg-preview-ok"
    assert calls == [("telegram_chat_raw", str(preview), "123")]
    assert "compressed preview video" in sent_texts[0]
    assert str(exported) not in sent_texts[0]


def test_send_telegram_video_passes_dimensions_duration_and_streaming(tmp_path: Path, monkeypatch):
    video = tmp_path / "result.mp4"
    video.write_bytes(b"video")
    observed: dict[str, object] = {}

    class FakeMessage:
        message_id = 42

    class FakeBot:
        async def send_video(self, **kwargs):
            observed.update(kwargs)
            return FakeMessage()

    class FakeAdapter:
        _bot = FakeBot()

    async def fake_send_with_adapter(call):
        return await call(FakeAdapter())

    monkeypatch.setattr(notifier, "_telegram_send_with_adapter", fake_send_with_adapter)
    monkeypatch.setattr(notifier, "_video_metadata", lambda path: {"width": 1080, "height": 1920, "duration": 12.4})

    result = notifier._run_async_send(lambda: notifier._send_telegram_video_attachment("6309747903", str(video), "123"))

    assert result["success"] is True
    assert result["message_id"] == "42"
    assert observed["chat_id"] == 6309747903
    assert observed["message_thread_id"] == 123
    assert observed["width"] == 1080
    assert observed["height"] == 1920
    assert observed["duration"] == 12
    assert observed["supports_streaming"] is True


def test_notify_terminal_job_reports_qq_video_send_failure(tmp_path: Path, monkeypatch):
    exported = tmp_path / "result.mp4"
    exported.write_bytes(b"video")
    sent_texts: list[str] = []

    async def fake_send_video(chat_id: str, file_path: str, chat_type: str = "") -> dict:
        return {"success": False, "error": "QQ Bot API error [400]"}

    async def fake_send_text(chat_id: str, message: str, chat_type: str = "") -> dict:
        sent_texts.append(message)
        return {"success": True, "message_id": "qq-text-ok"}

    monkeypatch.setenv("QQ_APP_ID", "app")
    monkeypatch.setenv("QQ_CLIENT_SECRET", "secret")
    monkeypatch.setattr(notifier, "_send_qq_video_attachment", fake_send_video)
    monkeypatch.setattr(notifier, "_send_qq_text", fake_send_text)

    result = notifier.notify_terminal_job(
        {
            "job_id": "cassette_test",
            "status": "succeeded",
            "outputs": [{"local_path": str(exported), "download": exported.name}],
            "delivery": {"platform": "qqbot", "chat_id": "qq_openid_raw"},
        }
    )

    assert result["status"] == "partial"
    assert result["platform"] == "qqbot"
    assert result["code"] == "qqbot_video_send_failed"
    assert result["message_id"] == "qq-text-ok"
    assert "QQ视频发送失败" in sent_texts[0]
    assert "qq_openid_raw" not in sent_texts[0]


def test_notify_progress_snapshot_sends_qq_image_without_raw_id(tmp_path: Path, monkeypatch):
    screenshot = tmp_path / "progress.png"
    screenshot.write_bytes(b"png")
    calls: list[tuple[str, str, str, str]] = []

    async def fake_send_image(chat_id: str, file_path: str, caption: str = "", chat_type: str = "") -> dict:
        calls.append((chat_id, file_path, caption, chat_type))
        return {"success": True, "message_id": "qq-image-ok", "mode": "native"}

    monkeypatch.setenv("QQ_APP_ID", "app")
    monkeypatch.setenv("QQ_CLIENT_SECRET", "secret")
    monkeypatch.setattr(notifier, "_send_qq_image_attachment", fake_send_image)

    result = notifier.notify_progress_snapshot(
        {
            "job_id": "cassette_test",
            "current_stage": "agent",
            "stage_timings": {"agent": {"duration_sec": 180.0, "attempts": 1}},
            "delivery": {"platform": "qqbot", "chat_id": "qq_openid_raw", "chat_type": "dm"},
        },
        str(screenshot),
        "Task Checklist running",
    )

    assert result["status"] == "sent"
    assert result["message_id"] == "qq-image-ok"
    assert calls[0][0] == "qq_openid_raw"
    assert calls[0][1] == str(screenshot)
    assert "Task Checklist" not in calls[0][2]
    assert "页面总结" not in calls[0][2]
    assert "qq_openid_raw" not in calls[0][2]


def test_notify_progress_snapshot_sends_telegram_image_with_english_caption(tmp_path: Path, monkeypatch):
    screenshot = tmp_path / "progress.png"
    screenshot.write_bytes(b"png")
    calls: list[tuple[str, str, str, str | None]] = []

    async def fake_send_image(chat_id: str, file_path: str, caption: str = "", thread_id: str | None = None) -> dict:
        calls.append((chat_id, file_path, caption, thread_id))
        return {"success": True, "message_id": "tg-image-ok", "mode": "native"}

    monkeypatch.setattr(notifier, "_send_telegram_image_attachment", fake_send_image)

    result = notifier.notify_progress_snapshot(
        {
            "job_id": "cassette_test",
            "cassette_language": "en",
            "current_stage": "agent",
            "stage_timings": {"agent": {"duration_sec": 180.0, "attempts": 1}},
            "delivery": {"platform": "telegram", "chat_id": "telegram_chat_raw", "thread_id": "topic_raw"},
        },
        str(screenshot),
        "Task Checklist running",
    )

    assert result["status"] == "sent"
    assert result["message_id"] == "tg-image-ok"
    assert calls[0][0] == "telegram_chat_raw"
    assert calls[0][1] == str(screenshot)
    assert calls[0][3] == "topic_raw"
    assert "Current page screenshot attached" in calls[0][2]
    assert "Task Checklist" not in calls[0][2]
    assert "telegram_chat_raw" not in calls[0][2]


def test_notify_model_selection_sends_quick_qq_text(monkeypatch):
    sent: list[tuple[str, str, str]] = []

    async def fake_send_text(chat_id: str, message: str, chat_type: str = "") -> dict:
        sent.append((chat_id, message, chat_type))
        return {"success": True, "message_id": "model-ok"}

    monkeypatch.setenv("QQ_APP_ID", "app")
    monkeypatch.setenv("QQ_CLIENT_SECRET", "secret")
    monkeypatch.setattr(notifier, "_send_qq_text", fake_send_text)

    result = notifier.notify_model_selection(
        {
            "job_id": "cassette_test",
            "model_selection": {"model": "Kimi K2.6", "thinking_level": "Medium"},
            "delivery": {"platform": "qqbot", "chat_id": "qq_openid_raw", "chat_type": "dm"},
        }
    )

    assert result["status"] == "sent"
    assert result["message_id"] == "model-ok"
    assert sent[0][0] == "qq_openid_raw"
    assert "Kimi K2.6" in sent[0][1]
    assert "思考程度：中" in sent[0][1]
    assert "qq_openid_raw" not in sent[0][1]


def test_notify_model_selection_sends_quick_telegram_text(monkeypatch):
    sent: list[tuple[str, str, str | None]] = []

    async def fake_send_text(chat_id: str, message: str, thread_id: str | None = None) -> dict:
        sent.append((chat_id, message, thread_id))
        return {"success": True, "message_id": "tg-model-ok"}

    monkeypatch.setattr(notifier, "_send_telegram_text", fake_send_text)

    result = notifier.notify_model_selection(
        {
            "job_id": "cassette_test",
            "cassette_language": "en",
            "model_selection": {"model": "DeepSeek V4 Flash", "thinking_level": "Low"},
            "delivery": {"platform": "telegram", "chat_id": "telegram_chat_raw", "thread_id": "topic_raw"},
        }
    )

    assert result["status"] == "sent"
    assert result["message_id"] == "tg-model-ok"
    assert sent[0][0] == "telegram_chat_raw"
    assert sent[0][2] == "topic_raw"
    assert "Cassette model selected: DeepSeek V4 Flash" in sent[0][1]
    assert "telegram_chat_raw" not in sent[0][1]


def test_qq_direct_senders_mark_short_lived_adapter_connected(tmp_path: Path, monkeypatch):
    calls: list[tuple[str, bool, str, str]] = []

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def aclose(self):
            return None

    class FakePlatformConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeSendResult:
        def __init__(self, success: bool, message_id: str = "", error: str = ""):
            self.success = success
            self.message_id = message_id
            self.error = error

    class FakeQQAdapter:
        def __init__(self, config):
            self.config = config
            self._chat_type_map = {}
            self._running = False
            self._ws = None
            self._http_client = None

        @property
        def is_connected(self):
            return bool(self._running and self._ws and not self._ws.closed)

        async def send(self, chat_id, message):
            calls.append(("text", self.is_connected, chat_id, message))
            return FakeSendResult(self.is_connected, "text-ok", "Not connected")

        async def send_video(self, chat_id, file_path):
            calls.append(("video", self.is_connected, chat_id, file_path))
            return FakeSendResult(self.is_connected, "video-ok", "Not connected")

        async def send_image_file(self, chat_id, file_path, caption=""):
            calls.append(("image", self.is_connected, chat_id, file_path))
            return FakeSendResult(self.is_connected, "image-ok", "Not connected")

    gateway_mod = types.ModuleType("gateway")
    platforms_mod = types.ModuleType("gateway.platforms")
    qqbot_mod = types.ModuleType("gateway.platforms.qqbot")
    config_mod = types.ModuleType("gateway.config")
    limits_mod = types.ModuleType("gateway.platforms._http_client_limits")
    adapter_mod = types.ModuleType("gateway.platforms.qqbot.adapter")
    httpx_mod = types.ModuleType("httpx")
    config_mod.PlatformConfig = FakePlatformConfig
    limits_mod.platform_httpx_limits = lambda: None
    adapter_mod.QQAdapter = FakeQQAdapter
    httpx_mod.AsyncClient = FakeAsyncClient
    for name, module in {
        "gateway": gateway_mod,
        "gateway.config": config_mod,
        "gateway.platforms": platforms_mod,
        "gateway.platforms._http_client_limits": limits_mod,
        "gateway.platforms.qqbot": qqbot_mod,
        "gateway.platforms.qqbot.adapter": adapter_mod,
        "httpx": httpx_mod,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    monkeypatch.setenv("QQ_APP_ID", "app")
    monkeypatch.setenv("QQ_CLIENT_SECRET", "secret")
    video = tmp_path / "result.mp4"
    image = tmp_path / "progress.png"
    video.write_bytes(b"video")
    image.write_bytes(b"png")

    text_result = notifier._run_async_send(lambda: notifier._send_qq_text("qq_openid_raw", "hello", "dm"))
    video_result = notifier._run_async_send(
        lambda: notifier._send_qq_video_attachment("qq_openid_raw", str(video), "dm")
    )
    image_result = notifier._run_async_send(
        lambda: notifier._send_qq_image_attachment("qq_openid_raw", str(image), "caption", "dm")
    )

    assert text_result["success"] is True
    assert video_result["success"] is True
    assert image_result["success"] is True
    assert [call[0] for call in calls] == ["text", "video", "image"]
    assert all(call[1] is True for call in calls)


def _mcp_desktop_env(monkeypatch, platform: str):
    monkeypatch.setenv("CASSETTE_RUNTIME_ADAPTER", "mcp")
    monkeypatch.delenv("CASSETTE_MCP_NOTIFY", raising=False)
    monkeypatch.setattr(notifier.sys, "platform", platform)


def test_notify_terminal_job_posts_macos_desktop_notification_under_mcp(monkeypatch):
    _mcp_desktop_env(monkeypatch, "darwin")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(notifier.subprocess, "run", fake_run)
    result = notifier.notify_terminal_job(
        {"job_id": "cassette_x", "status": "succeeded", "outputs": [{"local_path": "/exports/final.mp4"}]}
    )
    assert result == {"status": "sent", "platform": "desktop", "mode": "osascript"}
    assert calls[0][0] == "osascript"
    assert calls[0][-2] == "Edit finished: final.mp4"
    assert calls[0][-1] == "Oh My Cassette"


def test_notify_terminal_job_posts_linux_desktop_notification_for_needs_user(monkeypatch):
    _mcp_desktop_env(monkeypatch, "linux")
    monkeypatch.setattr(notifier.shutil, "which", lambda name: "/usr/bin/notify-send")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(notifier.subprocess, "run", fake_run)
    result = notifier.notify_terminal_job({"job_id": "cassette_y", "status": "needs_user"})
    assert result == {"status": "sent", "platform": "desktop", "mode": "notify-send"}
    assert calls[0] == ["notify-send", "Oh My Cassette", "Cassette needs your input (cassette_y)"]


def test_desktop_notification_can_be_disabled_and_skips_unsupported_platforms(monkeypatch):
    _mcp_desktop_env(monkeypatch, "darwin")
    monkeypatch.setenv("CASSETTE_MCP_NOTIFY", "0")
    assert notifier.notify_terminal_job({"job_id": "j", "status": "failed"}) == {
        "status": "skipped",
        "reason": "disabled",
    }
    _mcp_desktop_env(monkeypatch, "win32")
    result = notifier.notify_terminal_job({"job_id": "j", "status": "failed"})
    assert result["status"] == "skipped" and result["reason"] == "unsupported_desktop"


def test_desktop_notification_failure_is_reported_not_raised(monkeypatch):
    _mcp_desktop_env(monkeypatch, "darwin")

    def fake_run(cmd, **kwargs):
        raise OSError("osascript missing")

    monkeypatch.setattr(notifier.subprocess, "run", fake_run)
    result = notifier.notify_terminal_job({"job_id": "j", "status": "cancelled"})
    assert result == {
        "status": "failed",
        "platform": "desktop",
        "code": "desktop_notify_failed",
        "error": "OSError",
    }


def test_gateway_notifications_keep_platform_routing_without_mcp_adapter(monkeypatch):
    monkeypatch.delenv("CASSETTE_RUNTIME_ADAPTER", raising=False)
    result = notifier.notify_terminal_job({"job_id": "j", "status": "failed", "delivery": {}})
    assert result["status"] == "skipped"
    assert result["reason"] == "unsupported_platform"


def test_final_message_carries_live_link_and_delta():
    from cassette import notifier

    job = {
        "job_id": "cassette_x",
        "status": "needs_user",
        "quality": {},
        "delivery": {"platform": "telegram"},
        "editor_url": "http://127.0.0.1:8080/try?projectSessionId=abc&chatSessionId=u1",
        "timeline_delta": "CHANGES v41 -> v42  (1 change)\n~ V1/B beach.mp4  duration 00:13.8 -> 00:11.3",
    }
    message = notifier.format_platform_final_message(job, platform="telegram")
    assert "Watch live: http://127.0.0.1:8080/try?projectSessionId=abc" in message
    assert "CHANGES v41 -> v42" in message

    # Succeeded jobs keep the link but drop the mid-run delta block.
    done = {**job, "status": "succeeded", "outputs": []}
    message = notifier.format_platform_final_message(done, platform="telegram")
    assert "Watch live:" in message
    assert "CHANGES" not in message

    # zh platforms get the zh label.
    zh = notifier.format_platform_final_message({**job, "delivery": {"platform": "qqbot"}}, platform="qqbot")
    assert "实时查看：" in zh
