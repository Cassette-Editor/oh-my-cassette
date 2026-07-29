---
name: cassette-video-edit
description: Edit project media through the local Oh My Cassette MCP tools in Codex or Claude — a direct multi-turn conversation with the Cassette agent, with timeline previews, guided questions, and explicit export.
version: 2.0.0
metadata:
  tags: [cassette, video, codex, claude, mcp, media-editing]
  category: media
---

# Oh My Cassette local workflow

Use this skill when the user asks Codex or Claude to edit, cut, caption, reframe, subtitle, combine, polish, add music to, or export video, image, or audio media through Cassette.

The `cassette` MCP server is a local stdio child process. It opens no port. It sends media and edit requests directly to the separate Cassette backend. Do not start or depend on the repository's FastAPI web-demo server for this workflow.

## Courier doctrine

You are a courier between the user and the Cassette agent, not an editor or a brief writer.

- Pass the user's editing words to `cassette_run_job` as `message` VERBATIM — never rewrite, optimize, summarize, translate, or expand them. The Cassette agent is the creative brain; it reads the session's uploaded media itself.
- Relay the agent's questions and plans back to the user verbatim too. You add only three things: the timeline delta, the version numbers, and the live editor link.
- Do not call `cassette_make_prompt` on the API transport — it is a legacy browser-transport brief builder.
- Never ask upfront about model, thinking level, optimization, or BGM. Defaults match the web editor. Change model/thinking only when the user asks, via `cassette_config`.

## Safety and identity

- Treat only files inside the active host project roots or explicitly configured media roots as ingestible. If `cassette_ingest_media` returns `source_path_not_allowed`, ask the user to move the file into the project or run the private setup command with `--allowed-root`.
- Never copy credentials into chat. If an affected tool returns `auth_required`, show its exact `error.details.setup_command` as a private terminal command — or `error.details.reset_password_command` if the user no longer has their password.
- A stored password that stopped working reports `auth_failed` (or `cassette_auth_failed`), either in `error.details` or inside `data.job.errors[]` on a failed job. Show that error's `reset_password_command` as a private terminal command. If its `credential_source` is `environment`, the password lives in environment variables instead, so tell the user to update those — running the reset would not take effect.
- Keep the returned `session_id` and `job_id`. Sessions are isolated by default. Hand a session or job to another host only when the user deliberately asks for a Codex/Claude handoff.
- Use only paths and resource links returned in `artifacts`. Never invent an export path or ask the MCP runtime to expose another local file.

## Staying current

The MCP `instructions` carry an `UPDATE AVAILABLE:` line when a newer Oh My Cassette release exists. When they do:

- Mention it once per session, with both version numbers, and then offer to run the command the line names.
- Run that command only after the user explicitly agrees, and never re-offer in the same session. It replaces the plugin on disk, so tell the user the new version applies after the host reloads (`/reload-plugins` in Claude Code, a new task in Codex).
- Never fabricate a version or an update command; if the line is absent, the install is current and the subject does not come up.

## Conversational editing (multi-turn)

One session is one continuous conversation with the Cassette agent on one persistent thread — the agent remembers every previous turn, and the `editor_url` deep link stays the same for the whole session.

1. Call `cassette_ingest_media` once for each source asset. Omit `session_id` on the first call so the runtime generates one, then reuse the returned value.
2. Call `cassette_list_assets` and confirm the intended files are present.
3. For every editing request, call `cassette_run_job` with `message` set to the user's verbatim words and the same `session_id`. The call returns when the turn is settled — no status polling. Follow-ups like "make that title bigger" need no context restating — the agent remembers the conversation.
4. A turn ends `succeeded` with the edit committed and NOTHING rendered: the envelope carries `timeline_delta`, `quality.timeline_ctl`, and a contact-sheet artifact — that is the per-turn preview. Relay the delta and name the versions ("v3→v7: trimmed the intro to 4.0s").
5. Pass `export=true` on a turn ONLY when the user expresses finish/export intent. That turn ends `review_required`; evaluate and call `cassette_review_completion` (only `decision=export` renders).
6. If BGM is explicitly requested, use `cassette_match_exact_bgm` (concrete title/artist), `jamendo_music_matcher` (configured mood/genre), or `cassette_match_bgm` (Free To Use fallback) — then continue the conversation.

## Model and thinking level

- `cassette_config(session_id)` shows the current choice and the available options; `cassette_config(session_id, model=…, thinking_level=…)` changes them (accepts a product id like `openai/gpt-5.6-luna` or a label like "GPT-5.6 Luna").
- The preference persists for the session and applies from the next turn — the same semantics as switching the model between turns in the web editor.
- GPT thinking levels are `off`, `minimal`, `low`, `medium`, `high`, and `xhigh`.
- The only user-selectable models are GPT-5.6 Luna and GPT-5.4 Mini. Defaults (GPT-5.6 Luna, low thinking) match the web editor. Do not ask upfront; change only on user request and confirm in one line.

## Typed progress handling

Treat the structured `phase` and `next_action` fields as authoritative. Do not decide routing, progress, or completion from keywords in prose.

`cassette_run_job` IS the wait. It returns when the turn is settled, streaming MCP progress notifications while the agent works, and answers with a terminal phase. One call per user turn — do not follow it with a status loop.

- `succeeded`: the turn is done, nothing rendered — relay the delta + preview and continue the conversation, or re-run with `export=true` when asked.
- `needs_user`: present the pending question, then call `cassette_answer_question` with the same `job_id` and the user's `response`. On hosts that support MCP elicitation the runtime may collect the answer itself and return the already-resumed result; treat the returned phase as authoritative and do not re-answer.
- `review_required` (export turns only): evaluate the full edit result and call `cassette_review_completion`. Rendering begins only when the explicit decision is `export`; use `continue`, `needs_user`, or `failed` when that is the validated outcome.
- `exported`: present validated `artifacts` and their MCP resource links. The runtime has already measured the finished file into `quality.export_qc` (duration, fps, resolution, audio span, `black_segments`, and `audio_levels` with `mean_dbfs`/`peak_dbfs`) — read those numbers instead of probing the export yourself, and raise it with the user only when a field contradicts the request.
- `failed`, `cancelled`, or `timed_out`: report the structured error and the runtime-derived next action. A `thread_busy` error means a run is already live on this session's thread (often started from the open editor tab) — wait and retry.
- `running` or `exporting`: the call was detached (`wait=false`) or interrupted. See recovery below.

### Recovery, not polling

`cassette_job_status` exists to re-attach to a job whose call did not return — the host restarted, the call was cancelled, or the turn was deliberately started with `wait=false`. Call it **once** to read where the job stands, then act on the phase it reports. It is not a progress loop, and a settled turn never needs it.

`job_id` is the durable handle for that recovery: API jobs persist private thread and interrupt metadata and can resume after Codex or Claude restarts. Browser-transport jobs can resume only while the same MCP process retains the browser session; after restart, surface `browser_session_lost` and start a new browser job if the user wants to continue.

A long edit does not need managing. Hosts that background long tool calls (Claude Code moves any call past two minutes into a background task) keep the session usable while the turn runs, and deliver the result when it lands.

## Timeline grounding and the live editor

- Every user-visible statement about project state comes from `cassette_timeline`, never from memory. Name the version in replies: "Quick edit v42→v43: trimmed the intro to 4.0s."
- Lane routing: when the ask names specific clips or values and needs at most a handful of operations, read `cassette_timeline` then use `cassette_edit` (requires `CASSETTE_DIRECT_EDIT=1`; pass `expected_version` from the read; a `stale_timeline` error means re-read and retry). When it needs watching footage, music sync, or a plan, use `cassette_run_job`.
- The session carries ONE stable `editor_url` — a live view of the real editor (timeline, scrubbing preview, plan-review card; zero render) for the entire conversation. Hand it to the user once at the first turn and again at questions/review; on desktop offer to open it (`open <url>` on macOS, `xdg-open` on Linux). Do not repeat it on every reply.
- Job envelopes carry `timeline_delta` (cumulative changes since the turn started) and `plan_progress`; relay the delta rather than re-describing the timeline.
- Preview escalation, one step per explicit user ask: text digest → contact sheet (`cassette_timeline` with `contact_sheet=true`) → the `editor_url` live view → full export. Never auto-render.
- Showing previews in a terminal host (Claude Code, Codex CLI): the timeline digest is text — when the user asks to see the timeline, reprint `quality.timeline_ctl` verbatim in a fenced code block in your reply, never a paraphrase. For the contact sheet or storyboard sheet, terminal hosts cannot render image results inline — print the sheet's `contact_sheet_uri` / `storyboard_sheet_uri` (`file://…`) on its own line: most terminals make it cmd+clickable, opening the real pixels in the system image viewer. On a remote/SSH host where a local `file://` link cannot resolve, give the `editor_url` deep link instead. Optionally also render an at-a-glance version in the terminal with `chafa -f symbols --size <cols>x<rows>` when chafa is available. Preview files are swept after ~30 days (`CASSETTE_ARTIFACT_TTL_DAYS`); exports are never auto-deleted.
- Plan review: with `CASSETTE_PLAN_REVIEW=user` (the MCP default) a job pauses with an `edit_plan_review` question — the plan itself, with each storyboard beat as a readable cell (no raw links). The envelope's quality also carries `storyboard` (typed beat cells) and `storyboard_sheet` (a tiled image of one source frame per planned beat — show it when the host can display images). Relay the plan verbatim with the link; answer via `cassette_answer_question` with `approve`, `revise <feedback>`, or `reject`. The user may instead decide in the open editor tab: a `resume_not_waiting_for_user` error means the tab answered first — just re-check status. Note: typing a fresh message in the open editor tab cancels an in-flight plugin turn (the tab takes over) — that is product behavior, not an error to retry.

## Cancellation and handoff

- Call `cassette_cancel_job` only when the user asks to stop the edit.
- For a deliberate host handoff, provide the exact `session_id` and active `job_id`; the receiving host should begin with `cassette_job_status` rather than ingesting or starting a duplicate job.
- Exported files remain under the shared Oh My Cassette data directory. Prefer the returned resource link or file URI rather than relocating the artifact.
