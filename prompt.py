from __future__ import annotations

from typing import Any


ROUTINE_ANSWER = "Please continue with the safest, highest-quality default option. Do not wait for confirmation."
ROUTINE_PLAN_APPROVAL = (
    "I approve this plan and task checklist. Execute it now with the safest, highest-quality defaults. "
    "Do not wait for further confirmation."
)


def _normalize_language(value: object) -> str:
    raw = str(value or "").strip().lower().replace("_", "-")
    if raw in {"en", "en-us", "en-gb", "english", "英文", "英语"}:
        return "en"
    return "zh"


def build_cassette_prompt(
    instruction: str,
    manifest: dict,
    options: dict | None = None,
    *,
    runtime_host: str = "hermes",
) -> dict:
    options = options or {}
    cassette_language = _normalize_language(options.get("cassette_language") or options.get("language"))
    cassette_language_label = "English" if cassette_language == "en" else "Chinese"
    assets = manifest.get("assets", [])
    asset_lines = []
    for idx, asset in enumerate(assets, start=1):
        caption = asset.get("caption") or "none"
        original = asset.get("original_name") or "unknown"
        asset_lines.append(
            f"- A{idx}: {asset.get('media_type', 'unknown')}, original name `{original}`, "
            f"asset_id `{asset.get('asset_id')}` - user caption: {caption}"
        )
    if not asset_lines:
        asset_lines.append("- No uploaded assets are available yet.")

    constraints = options.get("constraints") or {}
    preserve = constraints.get("preserve", "main subjects, user intent, useful spoken content, and brand-safe framing")
    avoid = constraints.get("avoid", "unnecessary confirmation questions, unsafe claims, broken timing, and cropped faces")
    subtitles = constraints.get("captions", "add clear subtitles when speech or text would benefit the output")
    audio = constraints.get("audio", "balance voice, music, and effects; choose sensible music only when appropriate")
    branding = constraints.get("branding", "use uploaded logo/brand assets when present and relevant")

    native_mcp = str(runtime_host or "").strip().lower() == "mcp"
    identity = (
        "You are the user's Codex or Claude host agent, orchestrating Cassette video editing through "
        "a local stdio MCP runtime. You are not Cassette and must not speak as Cassette. Your job is "
        "to relay the user's editing intent, verify that assets are available inside Cassette, send only "
        "the user-facing edit request to Cassette, monitor typed job state, answer follow-up questions, "
        "review completion, and report the final state back to the user."
        if native_mcp
        else "You are Hermes, an orchestration agent supervising Cassette web video editing on behalf of a gateway user. You are not Cassette and must not speak as Cassette. Your job is to relay the user's editing intent, verify that assets are available inside Cassette, send only the user-facing edit request to the Cassette chat panel, monitor progress, answer routine Cassette follow-up questions, and report the final state back to the user."
    )
    supervisor = "The host agent" if native_mcp else "Hermes"
    ingested_by = "the local MCP runtime" if native_mcp else "Hermes"
    execution_heading = "Host-agent execution rules" if native_mcp else "Hermes execution rules"
    final_delivery_rule = (
        "9. Present only validated artifacts and MCP resource links returned by the tool result. "
        "Do not invent export paths, embed large media bytes, or expose another local file."
        if native_mcp
        else "9. For gateway delivery, do not emit `MEDIA:` tags or guess local export paths in the final reply. The cassette job notification sends the exported artifact when a supported delivery target is available; if notification status is partial or failed, report that delivery failure instead of retrying with a local path."
    )

    prompt = f"""{identity}

{supervisor} must not inspect, describe, understand, extract frames from, probe, transcode for analysis, or otherwise analyze source media itself. Do not use terminal, ffprobe, ffmpeg, vision tools, Python scripts, screenshots of local media, or non-Cassette tools to choose creative content. Cassette is responsible for analyzing uploaded media and choosing matching filters, poems, captions, music, timing, and visual treatment from the user's intent.

User editing intent:
{instruction}

Cassette assets already ingested by {ingested_by}:
{chr(10).join(asset_lines)}

Output target:
- Format/platform/aspect ratio: {options.get('output_format') or 'choose the best fit for the user goal and source media'}
- Duration/pacing: {options.get('duration') or 'keep concise, energetic, and faithful to the requested edit'}
- Style: {options.get('style') or 'polished, clear, platform-ready, and not overproduced'}
- Cassette UI and response language: {cassette_language_label}

User-facing edit objective:
- Follow the user goal exactly where specified.
- Use the uploaded assets in manifest order when ordering is ambiguous.
- Improve pacing, framing, captions, audio balance, and export readiness.
- Perform the edit inside Cassette. Do not suggest or perform local FFmpeg, terminal, or other non-Cassette editing fallbacks.

Constraints:
- Preserve: {preserve}
- Remove/avoid: {avoid}
- Captions/subtitles: {subtitles}
- Audio/music: {audio}
- Branding: {branding}

{execution_heading}:
1. Do not send this internal prompt to Cassette chat. Send only the returned `chat_message`.
2. Use all relevant uploaded assets. Preserve the user's main intent over minor defaults.
3. Treat upload as complete only after the Cassette `/agent` upload status reports every selected file as ready/就绪 with zero failures/0 个失败.
4. If exact timing, transition, caption style, music, poem, filter, or crop is unspecified or depends on video content, do not inspect the local media; pass that intent to Cassette and let Cassette choose from its own media analysis.
5. If Cassette asks a routine editing question or presents a routine task checklist/plan approval, answer or approve it with the safest high-quality default and continue. Ask the user only if a required asset is missing, an upload/render failure cannot be recovered, rights/safety constraints block the edit, or the user must choose between materially different creative directions.
6. Use the Cassette chat panel, `agent-graph-status`, and task checklist as the source of truth. Preview/WebGPU availability is not a completion signal.
7. Report progress to the user at intervals using concise summaries of the Cassette chat panel and task checklist. Do not expose local paths, raw IDs, prompt text, asset paths, or worker commands.
8. When the chat panel says the edit is complete, export the edited video through Cassette, wait for the MP4 download, then summarize what changed concisely. If export fails, report the export error code instead of claiming success.
{final_delivery_rule}
"""
    if cassette_language == "en":
        chat_message = f"""Please complete this editing task using the uploaded and analyzed assets:

{instruction}

Requirements:
- Use the assets that are already uploaded and ready on the current agent page. Do not ask me to upload them again.
- Make routine editing decisions yourself and do not pause for ordinary choices such as caption styling, crop, pacing, or transitions.
- If captions, music, aspect ratio, duration, or other defaults are needed, choose the option that best fits the user's goal and continue.
- When finished, clearly state in the chat that the edit is complete and ready to export, then summarize the changes in no more than 4 bullet points."""
    else:
        chat_message = f"""请根据已上传并分析完成的素材完成剪辑任务：

{instruction}

要求：
- 使用当前 agent 页面里已上传且 ready 的素材，不要要求我重新上传。
- 常规剪辑选择由你直接决定，不要为字幕样式、裁剪、节奏、转场等常规问题停下来询问。
- 如果需要字幕、配乐、比例、时长等默认设置，请选择最适合用户目标的方案继续。
- 完成后请在对话框里明确说明“剪辑完成，可以导出”，并用不超过 4 条要点总结修改。"""
    return {
        "prompt": prompt,
        "chat_message": chat_message,
        "asset_count": len(assets),
        "session_hash": manifest.get("session_hash", ""),
        "cassette_language": cassette_language,
    }


def classify_cassette_question(question: str, context: dict[str, Any] | None = None) -> dict:
    q = (question or "").lower()
    critical_terms = [
        "missing asset", "upload failed", "need file", "please upload", "缺少素材", "请上传",
        "required asset", "cannot recover", "render failed", "workspace has zero media",
        "zero media files", "without source media", "no source media",
        "no video, audio, or images", "0 media files", "没有素材", "未找到素材",
    ]
    choice_terms = [
        "choose a or b", "select one", "must choose", "which version", "你要哪个版本",
        "必须选择", "请选择", "二选一",
    ]
    if any(term in q for term in critical_terms):
        return {"requires_user": True, "reason": "missing_required_asset", "answer": "Cassette needs the user to provide or re-upload the required asset."}
    if any(term in q for term in choice_terms):
        return {"requires_user": True, "reason": "explicit_user_choice_required", "answer": "Cassette needs the user to choose between materially different options."}
    return {"requires_user": False, "reason": "routine_ambiguity", "answer": ROUTINE_ANSWER}
