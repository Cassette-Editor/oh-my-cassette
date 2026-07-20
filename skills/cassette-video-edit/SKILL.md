---
name: cassette-video-edit
description: Edit project media through the local Oh My Cassette MCP tools in Codex or Claude, including guided choices, background monitoring, questions, review, and export.
version: 1.0.0
metadata:
  tags: [cassette, video, codex, claude, mcp, media-editing]
  category: media
---

# Oh My Cassette local workflow

Use this skill when the user asks Codex or Claude to edit, cut, caption, reframe, subtitle, combine, polish, add music to, or export video, image, or audio media through Cassette.

The `cassette` MCP server is a local stdio child process. It opens no port. It sends media and edit requests directly to the separate Cassette backend. Do not start or depend on the repository's FastAPI web-demo server for this workflow.

## Safety and identity

- Treat only files inside the active host project roots or explicitly configured media roots as ingestible. If `cassette_ingest_media` returns `source_path_not_allowed`, ask the user to move the file into the project or run the private setup command with `--allowed-root`.
- Never copy credentials into chat. If an affected tool returns `auth_required`, show its exact `error.details.setup_command` as a private terminal command.
- Keep the returned `session_id` and `job_id`. Sessions are isolated by default. Hand a session or job to another host only when the user deliberately asks for a Codex/Claude handoff.
- Use only paths and resource links returned in `artifacts`. Never invent an export path or ask the MCP runtime to expose another local file.

## Guided edit

1. Call `cassette_ingest_media` once for each source asset. Omit `session_id` on the first call so the runtime generates one, then reuse the returned value.
2. Call `cassette_list_assets` and confirm the intended files are present.
3. Before the first run, ask concise guided choices that are not already answered: Cassette model, thinking level, whether to optimize the instruction, and whether to add smart BGM. Preserve explicit aspect ratio, duration, caption text, ordering, brand, exclusion, and output requirements.
4. If BGM is requested, use the provider flow appropriate to the user's choice:
   - `cassette_match_exact_bgm` for a concrete title and artist;
   - `jamendo_music_matcher` for fixed-form mood/genre preferences when configured;
   - `cassette_match_bgm` for Free To Use category/tag queries and deterministic fallback.
5. Call `cassette_make_prompt` with the confirmed original or optimized instruction.
6. Call `cassette_run_job` with its `data.prompt`, `data.chat_message`, the same session, model, thinking level, and language. Leave `wait` omitted or false for the normal background path.

## Typed progress handling

Treat the structured `phase` and `next_action` fields as authoritative. Do not decide routing, progress, or completion from keywords in prose.

- `running` or `exporting`: call `cassette_job_status` with `wait_for_change_sec=30`.
- `needs_user`: present the pending question, then call `cassette_answer_question` with the same `job_id` and the user's `response`. On hosts that support MCP elicitation, `cassette_job_status` may collect the answer itself and return the already-resumed status; treat the returned phase as authoritative and do not re-answer.
- `review_required`: evaluate the full edit result and call `cassette_review_completion`. Rendering begins only when the explicit decision is `export`; use `continue`, `needs_user`, or `failed` when that is the validated outcome.
- `exported` or `succeeded`: present validated `artifacts` and their MCP resource links.
- `failed`, `cancelled`, or `timed_out`: report the structured error and the runtime-derived next action.

The named monitoring budget is `CASSETTE_MCP_MONITOR_BUDGET_SEC`, defaulting to 1500 seconds. Use 30-second long-polls until a phase changes or that elapsed-time budget is reached. If it is still running when the budget expires, return the live `job_id` and explain that the edit continues in the background. Do not tight-poll.

API jobs persist private thread and interrupt metadata and can resume after Codex or Claude restarts. Browser-transport jobs can resume only while the same MCP process retains the browser session; after restart, surface `browser_session_lost` and start a new browser job if the user wants to continue.

## Cancellation and handoff

- Call `cassette_cancel_job` only when the user asks to stop the edit.
- For a deliberate host handoff, provide the exact `session_id` and active `job_id`; the receiving host should begin with `cassette_job_status` rather than ingesting or starting a duplicate job.
- Exported files remain under the shared Oh My Cassette data directory. Prefer the returned resource link or file URI rather than relocating the artifact.
