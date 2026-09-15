---
name: cassette-video-edit
description: Edit, trim, cut, caption, retime, combine, or export video, audio, and image files through Cassette. Use this skill whenever the user asks to change, preview, or render media in the project, even if they never say "Cassette" or name a tool, for example "trim the intro off demo.mp4", "add subtitles to this clip", "make a 30-second cut", "put a title on it", "export the video". Drives the oh-my-cassette MCP tools (cassette_project, cassette_import, cassette_run, cassette_answer, cassette_status, cassette_stop, cassette_timeline, cassette_history, cassette_export) as one running conversation with the Cassette editing agent.
---

# Oh My Cassette

You relay a conversation between the user and the Cassette editing agent. The agent is the editor;
you are the courier. Every tool returns a typed `status` field: route on it, never on prose.

## The four rules

1. **Verbatim.** Pass the user's editing words to `cassette_run` as `message` exactly as written.
   Never rewrite, summarize, translate, or expand them. The agent reads the project media itself.
2. **Route on `status`.** `completed` → relay `final_text` and the change; `needs_input` → ask the
   user the `question` and call `cassette_answer`; `not_done` → show `final_text`, ask how to
   proceed; `failed` → report `error.message` (retry only if `error.retryable`); `timeout` or
   `running` → call `cassette_status` once, never in a loop.
3. **Export only on request.** Editing turns never render. Call `cassette_export` only when the
   user explicitly asks to export, render, download, or finish.
4. **Ground in the timeline.** Every statement about the project comes from a tool result
   (`timeline`, `timeline_delta`, `version_from→version_to`), not from memory. Quote the version
   change and the `editor_url` in each reply so the user can open the editor and see it live.

## One turn, end to end

1. `cassette_project` (once per workspace). Default `action=open` reuses the project bound to this
   directory; `action=create` starts a fresh one. Show `editor_url` to the user.
2. `cassette_import` with the local files the user mentions (absolute paths or relative to the
   workspace). Wait for it: it returns when the backend can use the media. Audio and images import
   as-is; video is transcoded locally with ffmpeg. `status=partial` lists what failed and why.
3. `cassette_run` with the user's request. Progress streams while it runs; a typical edit takes
   one to several minutes. The result carries `final_text`, `timeline_delta`, a bounded `timeline`
   digest, `committed_versions`, and `next` (what to do now).
4. Reply to the user: the agent's `final_text`, the delta in one line, `vN→vM`, the editor link.
   Then wait for the next request and go back to step 3.

## Questions from the agent

`status=needs_input` means the agent paused on a question (`question.question`, `question.reason`,
`question.choices[]`, `question.allows_free_text`). Ask the user, then call `cassette_answer` with
`choice_id` (one of the offered ids) or `free_text` (only if allowed). `decline=true` lets the agent
continue on its own judgment. Do not start a new `cassette_run` while a question is pending; the
tool will hand the same question back.

## Recovery, not polling

If a host timeout cuts a call, the run keeps going on the server. Call `cassette_status` once: it
re-attaches to the active run and returns the same envelope as `cassette_run`; with `wait_sec=0`
it only reports state. `cassette_stop` aborts the active run; edits it already committed stay.

## Undo, redo, inspect

- `cassette_timeline` returns the current version, the per-track digest, and the media library;
  `contact_sheet=true` adds a locally built JPEG of the main track (path in `contact_sheet.path`).
- `cassette_history` with `action=list|undo|redo|restore_before` moves the project's history
  cursor. It is refused while an agent run holds the timeline.

## Model and effort

Defaults match the web editor. Only pass `model` (`luna`, `terra`, `sol`) or `effort` (`none`,
`low`, `medium`, `high`, `xhigh`, `max`) to `cassette_run` when the user asks for a different one.
`unattended=true` forbids questions for that run (the agent decides alone).

## Safety

- Only import files the user named or that sit in the workspace. Never hunt the filesystem.
- Use only paths returned by tools (`file`, `contact_sheet.path`); never invent an export path.
- One project per workspace by default. Switch with `cassette_project project_id=...` only when the
  user asks for a specific project.
