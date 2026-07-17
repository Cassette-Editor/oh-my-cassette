---
name: cassette-video-edit
description: Orchestrate gateway media and natural-language editing instructions into a complete Cassette web-editing job using the cassette plugin tools.
version: 1.0.0
metadata:
  hermes:
    tags: [cassette, video, gateway, qqbot, telegram, browser-automation, media]
    category: automation
    requires_toolsets: [cassette, browser]
---
# Cassette Video Edit Workflow

## When to Use

Use this skill when a gateway user, including QQ or Telegram, asks Hermes to edit, cut, caption, reframe, remix, subtitle, combine, polish, export, or otherwise transform video/image/audio media through the Cassette agent page at `https://sg.trycassette.online/agent` by default. Weixin/WeChat remains a legacy compatible path, but QQ and Telegram are the primary supported gateway targets.

Also use it when the user sends media first and later gives an editing instruction, or gives an instruction first and then uploads required assets. The goal is to make Cassette continue working with a complete prompt rather than stopping for routine clarification.

Use Cassette as the only editing engine. Do not fall back to local FFmpeg, terminal scripts, browser-native media editing, or any other non-Cassette editing path unless the user explicitly cancels Cassette and asks for a different tool.

Hermes is not the creative video-analysis engine. For editing requests, Hermes must not inspect, describe, understand, extract frames from, probe, or analyze the source media itself. Do not use `terminal`, `ffprobe`, `ffmpeg`, `vision_analyze`, Python scripts, local screenshots/frames, or other non-Cassette tools to decide filters, poems, captions, music, timing, or visual treatment. Relay the user's intent to Cassette and let Cassette analyze the uploaded media.

## Procedure

1. Ingest incoming media immediately with `cassette_ingest_media`. If the turn contains only media or a gateway placeholder such as `[Attachment: ...]` / `[Voice] ...`, save the assets and wait for a later editing instruction instead of starting Cassette.
2. Inspect manifest with `cassette_list_assets`.
3. Interpret editing intent.
4. Before the first edit in a Hermes session, let the plugin ask the user to choose the Cassette model and thinking level from the live `/agent` model list. Later edits in the same Hermes session must reuse that saved choice and must not re-ask unless the user sends `/cassette_model`.
5. Before the first edit in a Hermes session, ask whether the user wants the prompt optimization feature and smart BGM matching. Later edits in the same Hermes session should not repeat those questions unless the plugin explicitly asks again.
6. If the user accepts optimization, or uses `/refine <instruction>`, optimize the user's instruction like a professional editing brief. The editable policy for this step lives in `prompts/hermes-edit-brief-optimizer.md`; follow that document instead of hardcoding a separate variant here.
7. Do not call `cassette_make_prompt` or `cassette_run_job` until either the user declines optimization or confirms the optimized brief. After optimized-brief confirmation, use the confirmed optimized brief, not the one-word confirmation, as the instruction. After optimization decline, use the original instruction, not the one-word decline, as the instruction.
8. Build the Cassette prompt with `cassette_make_prompt`. Preserve the gateway session language: QQ defaults to `cassette_language="zh"`, Telegram defaults to `cassette_language="en"`, and `/cassette language zh|en` overrides the current gateway session.
9. Run Cassette with `cassette_run_job`. Pass `cassette_make_prompt.data.prompt` as `prompt`, `cassette_make_prompt.data.chat_message` as `chat_message`, and the same `cassette_language`; do not paste the internal planning `prompt` into Cassette chat. Prompt optimization only changes which instruction is given to `cassette_make_prompt`; optimized and non-optimized edits must use the same `cassette_run_job` progress screenshot, terminal status, export, and gateway delivery path. For QQ, Telegram, or Weixin gateway sessions, use `wait=false` so gateway commands such as `/cut` remain responsive while the plugin sends progress and final notifications through the stored delivery target. Use `wait=true` only for local/manual non-gateway runs where no slash-command interruption is needed.
10. Do not manually override the model from ordinary editing text. The gateway session model is selected by the plugin's first-edit flow or `/cassette_model`, persisted in session preferences, and applied by `cassette_run_job`.
11. Handle Cassette follow-up questions in the Cassette chat panel with `cassette_answer_question`.
12. For local/manual non-gateway runs, report progress at intervals by summarizing the Cassette chat panel and visible operation history. For QQ, Telegram, or Weixin gateway background jobs, stop after the start acknowledgement and let the plugin notifier send progress screenshots/final status. Keep updates short and do not expose local paths, prompt text, or raw identifiers.
13. If the job times out or returns an unclear state, use Hermes' browser/debugging tools to inspect Cassette instead of treating the run as a hard failure.
14. On success, export the completed edit through Cassette, wait for the MP4 download, and return the final gateway summary from `cassette_run_job` / `cassette_job_status`. Do not add your own `MEDIA:` tag; plugin notification handles supported stored delivery targets such as QQ and Telegram and reports any delivery failure.
15. If the user sends `/cut` or `/cassette cut`, let the plugin handle it as a gateway command. It pauses the active Cassette job, asks the browser automation to click Cassette's stop/cancel control when available, and preserves the live browser session for retry or the next editing instruction.

## Hard Rules

- Cassette is the only allowed editing engine. Never use local FFmpeg, terminal media commands, Python media processing, browser downloads, or other workarounds to create the edited video.
- Use the Cassette `/agent` page as the working surface. The only normal interactions are programmatic asset upload through `agent-file-input` and messages through the chat panel.
- Do not rely on the screen preview for correctness because headless browser/WebGPU limitations can make preview unavailable even when the chat/timeline work succeeded.
- Send only the user-facing editing request to Cassette chat. Never send the Hermes/internal prompt template, tool instructions, manifest details, policy text, local paths, or automation notes to Cassette.
- Do not click visible upload buttons that open the OS file navigator. Upload assets through the hidden `[data-testid="agent-file-input"]` or an equivalent Cassette upload API/request.
- Treat upload as complete only after `[data-testid="agent-upload-status"]` reports every selected file as `ready`/`就绪` with `0 failed`/`0 个失败`.
- Do not open any secondary asset browser or try to manually add assets to a timeline; the new `/agent` page combines upload, transfer, analysis, and availability for the chat agent.
- Judge completion primarily from `[data-testid="chat-assistant-message"]`, `[data-testid="agent-graph-status"]`, `[data-testid="agent-task-tracker"]`, the language-neutral export button state, and the absence of an active stop control. A preview error is diagnostic context, not a failure by itself.
- Do not treat an enabled export button alone, or a single keyword in Cassette's reply, as proof that the current edit succeeded or failed. If `cassette_run_job` returns `needs_user` with `completion_requires_hermes_review`, Hermes must act as supervisor: inspect the latest Cassette assistant reply and live browser state, then call `cassette_review_completion` with `decision="export"`, `"continue"`, `"failed"`, or `"needs_user"` according to the full semantic state. Export only when the reply means the edit is complete enough to export.
- If Cassette presents a routine plan, task checklist, or continue/approve interaction, use browser DOM state and available controls to approve or continue it with sensible defaults. Do not stop for user confirmation or rely on screenshot interpretation for routine Cassette workflow approvals.
- After chat completion, use the Cassette export control, confirm export, and wait for the exported MP4 download before reporting success. If export fails, report the export error code and do not claim the video is ready.
- Do not invent a local export path from the downloaded filename. Scrubbed tool results intentionally hide `local_path`; exported files live under `${CASSETTE_ASSET_ROOT}/exports/<job_id>/`, and gateway delivery should be handled by the plugin notification path or by querying the persisted job JSON.
- Do not emit `MEDIA:` for exported Cassette videos in gateway replies. If `notification.status` is `partial` or `failed`, tell the user the plugin could not deliver the attachment instead of retrying the same mp4 path through gateway media routing.
- Do not say "I will look at/watch/analyze the video" for a Cassette edit. The correct response is to start the Cassette tool flow and report Cassette progress.
- Do not start a Cassette job from a media-only gateway turn. Save all videos, images, and audio files first; start the job only after a later user message provides an actual editing instruction.
- Do not alter explicit user requirements when optimizing the editing brief. If the user says a product name, price, duration, aspect ratio, caption text, layout constraint, style, order, or exclusion, preserve it. Improve only unspecified details.
- Do not assume prompt optimization is enabled. The first response after the first edit instruction in a Hermes session should ask whether to optimize after the plugin has captured the model choice. If the user declines, immediately run Cassette with the original instruction after the smart-BGM choice. If the user accepts, send the optimized brief plus a confirmation request; the Cassette job starts only after the user confirms the optimized brief. For later edits in the same session, follow the plugin rewrite instead of asking again. `/refine <instruction>` forces prompt optimization, and `/music <BGM requirement>` only adds a smart-matched BGM asset without starting Cassette.
- `/check_assets` is the only gateway command for reporting how many assets have been saved in the current session. Do not treat natural-language receive/status complaints such as "I didn't receive the video" as asset-check requests.
- `/cassette_model` changes the saved Cassette model/thinking preference for the current gateway session. The plugin asks for numbered choices; do not route those number replies through BGM selection, prompt optimization, or editing.
- `/cut` and `/cassette cut` are pause commands, not new editing instructions. Do not route them through prompt optimization, smart BGM, `cassette_make_prompt`, or a fresh `cassette_run_job`; the plugin will cancel the active job and keep the browser state alive.
- `/cassette language zh|en` sets the Cassette UI/reply language for the current gateway session. Do not treat it as an editing instruction, prompt-optimization reply, BGM reply, or confirmation.
- Smart BGM provider choice is controlled by the plugin rewrite. When the user accepts smart BGM, first recommend exactly 3 concrete real songs with title plus artist/singer, followed by option 4 "换一批" and option 5 "随机匹配"; tell the user they can reply 1-5 or send text to add BGM requirements and receive a new batch. Do not call any BGM tool before the user chooses. If the user selects 1-3, call `cassette_match_exact_bgm` with the selected title and artist. If the user selects 4, provide a new batch and avoid repeating previous recommendations. If the user sends non-number text, treat it as additional BGM requirements and recommend a new batch instead of scolding or reasking only for numbers. If the user selects 5, follow the plugin-selected random provider order. If exact song search fails, fall back without asking again: use `jamendo_music_matcher` when configured, then Free To Use `cassette_match_bgm`. Never generate or print raw Jamendo SearchPlan JSON in assistant content. Do not create local Jamendo rules, scoring, cache, weighted random, or extra providers.

## Browser Debug Fallback

Use the cassette plugin tools for the main workflow, but use Hermes' built-in browser tools as the first-line debugger after a timeout, `needs_user`, missing exported artifacts, or selector mismatch.

When `cassette_run_job` returns `timed_out`:

1. Read the scrubbed job result: status, error codes, output count, and final screenshot path.
2. If `final_screenshot` exists, inspect it with a visual/browser tool before replying to the user.
3. If the screenshot or live page shows a timeline, clips, subtitles, operation history, chat response, or generated content, report this as "Cassette created an edit but export/completion was not detected", not as total failure.
4. Use `browser_navigate` to open the Cassette URL and then `browser_snapshot` / `browser_console` to inspect chat panel state, operation history, broken selectors, upload status, export/download buttons, and client-side errors.
5. If the Cassette page shows a follow-up question, classify it with `cassette_answer_question` and answer when safe.
6. If the page shows an export/download control but no link, click the Cassette export action, confirm export, wait for progress/download, then re-check job outputs.
7. If the preview says the engine is unavailable but the chat panel or operation history shows work was performed, ignore the preview limitation for task progress and provide the job id plus the diagnostic state. Do not claim an exported video is ready until the export artifact is downloaded.
8. Do not inspect or manipulate media locally to "finish" the edit. Local filesystem checks are only for validating Cassette job metadata, screenshots, and exported artifacts produced by Cassette.

Prefer this fallback over rewriting the automation. The plugin should keep owning asset ingestion, prompt construction, job persistence, and status files; Hermes browser tools should inspect ambiguous web UI state and help discover selector changes.

## Prompt Template

```text
You are Hermes, an orchestration agent supervising Cassette web video editing on behalf of a gateway user. You are not Cassette and must not speak as Cassette. Your job is to relay the user's editing intent, verify that assets are available inside Cassette, send only the user-facing edit request to the Cassette chat panel, monitor progress, answer routine Cassette follow-up questions, and report the final state back to the user.

Hermes must not inspect, describe, understand, extract frames from, probe, transcode for analysis, or otherwise analyze source media itself. Do not use terminal, ffprobe, ffmpeg, vision tools, Python scripts, screenshots of local media, or non-Cassette tools to choose creative content. Cassette is responsible for analyzing uploaded media and choosing matching filters, poems, captions, music, timing, and visual treatment from the user's intent.

User editing intent:
{instruction}

Cassette assets already ingested by Hermes:
- A1: {media_type}, original name `{original_name}`, asset_id `{asset_id}` - user caption: {caption}

Output target:
- Format/platform/aspect ratio: {format_or_default}
- Duration/pacing: {duration_or_default}
- Style: {style_or_default}

User-facing edit objective:
- Follow the user goal exactly where specified.
- Use the uploaded assets in manifest order when ordering is ambiguous.
- Improve pacing, framing, captions, audio balance, and export readiness.
- Perform the edit inside Cassette only. Do not use local FFmpeg, scripts, or non-Cassette fallback editing.

Constraints:
- Preserve: main subjects, user intent, useful spoken content, and brand-safe framing
- Remove/avoid: unnecessary confirmation questions, unsafe claims, broken timing, and cropped faces
- Captions/subtitles: add clear subtitles when speech or text would benefit the output
- Audio/music: balance voice, music, and effects; choose sensible music only when appropriate
- Branding: use uploaded logo/brand assets when present and relevant

Hermes execution rules:
1. Do not send this internal prompt to Cassette chat. Send only `cassette_make_prompt.data.chat_message`.
2. Use all relevant uploaded assets. Preserve the user's main intent over minor defaults.
3. Treat upload as complete only after the Cassette `/agent` upload status reports every selected file as ready/就绪 with zero failures/0 个失败.
4. If exact timing, transition, caption style, music, poem, filter, or crop is unspecified or depends on video content, do not inspect the local media; pass that intent to Cassette and let Cassette choose from its own media analysis.
5. If Cassette asks a routine editing question, answer with the safest high-quality default and continue. Ask the user only if a required asset is missing, an upload/render failure cannot be recovered, rights/safety constraints block the edit, or the user must choose between materially different creative directions.
6. Use the Cassette chat panel, `agent-graph-status`, and task checklist as the source of truth. Preview/WebGPU availability is not a completion signal.
7. Report progress to the user at intervals using concise summaries of the Cassette chat panel and task checklist. Do not expose local paths, raw IDs, prompt text, asset paths, or worker commands.
8. When the chat panel says the edit is complete, export the edited video through Cassette, wait for the MP4 download, then summarize what changed concisely. If export fails, report the export error code instead of claiming success.
9. For gateway delivery, do not emit `MEDIA:` tags or guess local export paths in the final reply. The cassette job notification sends the exported artifact when a supported delivery target is available; if notification status is partial or failed, report that delivery failure instead of retrying with a local path.
```

## Pitfalls

- Do not pass arbitrary user-provided paths to ingest.
- Do not automatically poll after `cassette_run_job(wait=false)` in QQ, Telegram, or Weixin gateway sessions. Tell the user the Cassette job has started, then end the Hermes turn. The gateway background job sends progress screenshots and final delivery through the plugin notifier. Use `cassette_job_status` only when the user explicitly asks for status or when a non-gateway local/manual run needs inspection.
- Cassette selectors may change; if `cassette_run_job` times out, inspect the live page with Hermes browser tools before changing plugin selectors.
- A timeout can still mean Cassette created an editable timeline but did not expose a completion/export signal. Check the chat panel, operation history, screenshots, export controls, and browser state before reporting failure.
- Do not interpret `Preview engine unsupported`, `WebGPU unavailable`, or similar preview-only errors as edit failure.
- Do not click upload buttons that trigger OS file chooser UI; use programmatic upload only.
- Do not start chatting with Cassette immediately after selecting files. First wait until `agent-upload-status` says all files are ready/就绪 and none failed/0 个失败.
- Do not use the removed Collections/workspace-add flow on `/agent`; it is stale and can cause false failures.
- Do not send `cassette_make_prompt.data.prompt` to Cassette chat; send `cassette_make_prompt.data.chat_message`.
- Keep using the gateway-provided cassette `session_id` from the user/system context when calling `cassette_list_assets`, `cassette_make_prompt`, and `cassette_run_job`. Do not replace it with `manifest.session_id`, `manifest.session_hash`, or `manifest_path` from a tool result.
- Do not add `?new=true`, cache-busting query strings, fragments, or alternate Cassette URLs to retry a same-session job. The plugin owns browser session reuse and will keep the `/agent` page alive for the gateway-provided cassette `session_id` until the Hermes session ends, `/new` is used, Playwright cannot safely reuse the page, or Cassette reports an explicit hard page error such as request/network/server failure.
- When Cassette reports a request/network/server failure but the browser page remains controllable, the plugin first uses Cassette's `New Chat` control and retries without reuploading already-ready assets. Hermes should not manually restart the browser or append URL query strings for this case.
- Do not manually send a model-selection message yourself. `cassette_run_job` selects the Cassette model for the first submission in a live browser session and sends the platform notice through the stored delivery target.
- Do not retry by using local media tools. Cassette is the only allowed editor for this workflow.
- Do not run `ffprobe`, `ffmpeg`, frame extraction, `vision_analyze`, or similar tools to inspect source media before prompting Cassette, even when the user asks for content-dependent choices like "suitable filter", "matching poem", or "appropriate captions".
- Do not use `convert` or other unavailable image tools to inspect screenshots; use browser snapshot, browser screenshot, or report the saved screenshot path.
- Do not over-ask the user for stylistic choices.
- Do not expose local paths, raw wxids/openids, tokens, or the full prompt in user-facing replies.
- Do not emit `MEDIA:` for Cassette export delivery, including guessed paths such as `sessions/<hash>/output/<filename>.mp4` or discovered paths under `exports/<job_id>/`. Let the plugin notification path send the artifact and report its `notification.status`.
- Telegram Bot API uploads are limited to 50 MB for videos/files. Do not manually compress and resend Cassette exports from Hermes. The plugin notifier handles this: exports over `CASSETTE_TELEGRAM_VIDEO_MAX_BYTES` are converted into a compressed MP4 preview, sent with probed width/height/duration and `supports_streaming=true`, and the final message states that the original export remains under `${CASSETTE_ASSET_ROOT}/exports/<job_id>/...`.

## Verification

Before reporting success, verify manifest, upload completion, prompt submission, chat-panel completion, operation history, export completion, downloaded MP4 artifact, screenshot, and final summary. If the export artifact is missing, report the exact observed state: draft/timeline created, chat says complete, waiting for export, needs user, preview unavailable, page load failed, upload failed, chat input failed, export failed, timed out, or unknown.

## Progress Updates

For gateway users, let the plugin notifier send progress and final summaries instead of keeping Hermes in a polling loop:

- After job creation: tell the user the Cassette job has started and that progress/final notifications will be sent automatically.
- During work: do not call `cassette_job_status` repeatedly in the same Hermes turn. The plugin records upload, model selection, language selection, agent, and export stage timings, and sends Cassette page screenshots to QQ/Telegram during the agent stage every `CASSETTE_PROGRESS_SNAPSHOT_SEC` seconds by default.
- Explicit status requests: if the user asks for status, call `cassette_job_status` once and summarize its `report.current_stage`, `report.stage_timings`, latest `progress_events`, and `report.progress_snapshot_count`; then end the turn.
- On timeout: summarize what Cassette visibly accomplished and what is still pending.
- On completion: summarize changes and mention the plugin notification result. Do not add an extra `MEDIA:` tag; detached jobs may also send this final summary directly using the stored delivery target.
