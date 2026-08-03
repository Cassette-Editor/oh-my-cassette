# Showing the user what changed

Escalate one step per explicit user ask, and never auto-render:

**text digest → contact sheet → full export**

Every settled turn already carries the first level in its envelope, so most turns need nothing
extra from you.

## Timeline digest

The digest is text. When the user asks to see the timeline, reprint `quality.timeline_ctl`
**verbatim in a fenced code block**. It is a mechanical rendering of the project document, so
paraphrasing it silently invents state that nothing verified.

## Contact sheet and storyboard sheet

`cassette_timeline` with `contact_sheet=true` renders a contact sheet.

Terminal hosts (Claude Code, Codex CLI) cannot display image results inline, so print the sheet's
`contact_sheet_uri` / `storyboard_sheet_uri` (`file://…`) on its own line — most terminals make it
cmd+clickable, which opens the real pixels in the system image viewer.

On a remote or SSH host a local `file://` cannot resolve; say the sheet is not viewable there. Do
not offer an editor link as a substitute — it is a bearer capability any signed-in account that
sees it can act on.

Optionally also render an at-a-glance version inline with `chafa -f symbols --size <cols>x<rows>`
when `chafa` is available.

Preview files are swept after about 30 days (`CASSETTE_ARTIFACT_TTL_DAYS`). Exports are never
auto-deleted.

## Plan review

With `CASSETTE_PLAN_REVIEW=user` — the default on MCP hosts, because a person is present in the
chat — a job pauses with an `edit_plan_review` question carrying the plan itself, each storyboard
beat as a readable cell and no raw links. The envelope's `quality` also carries `storyboard` (typed
beat cells) and `storyboard_sheet` (a tiled image of one source frame per planned beat, produced
without rendering the timeline).

Relay the plan verbatim, then answer via `cassette_answer_question` with `approve`,
`revise <feedback>`, or `reject`.

The user may instead decide in an open editor tab, and first answer wins: a
`resume_not_waiting_for_user` error means the tab answered first, so re-check status rather than
retrying the answer. Separately, typing a fresh message in an open editor tab cancels an in-flight
plugin turn because the tab takes over — that is product behaviour, not an error to retry.
