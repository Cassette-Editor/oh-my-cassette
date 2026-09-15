# Development & Troubleshooting

[← Back to README](../README.md)

> [!TIP]
> Join our Discord community to connect with contributors and fellow `oh-my-cassette` users.
>
> [![Discord](https://img.shields.io/discord/1514649803626250452?style=for-the-badge&logo=discord&logoColor=white&label=Discord&labelColor=black&color=5865F2)](https://discord.gg/qd9NY4k8d7)

## How it fits together

```text
host (Claude Code / Codex / OpenCode / Hermes)
  └─ stdio ─ uvx oh-my-cassette            src/oh_my_cassette/server.py   9 tools + instructions
               ├─ tools/                     project · media · run · timeline · export
               ├─ app.py                     composition: settings, state, HTTP, clients
               ├─ cassette/                  HTTP + SSE client, pydantic mirrors of the backend contract
               │    projects · agent · media · export · prepare (ffmpeg) · auth
               ├─ render/                    timeline digest / delta, local contact sheet
               └─ state.py                   ~/.oh-my-cassette/state.json
                        │ HTTP + SSE
                        ▼
              Cassette-Editor backend        /api/projects · /api/agent · /api/media · /api/export
```

One editing turn (`cassette_run`):

1. Resolve the project: explicit `project_id`, else the binding for the working directory, else the
   most recently used project (`app.resolve`).
2. `POST /api/agent/sessions/<chat>/commands` with a `start` command (idempotency key derived from
   project, session, turn number and message; `expectedRevision` from `/state`, one retry on 409).
3. Follow `GET /api/agent/sessions/<chat>/events?after=<cursor>` (SSE). Durable events become
   progress notifications; `run_terminal` / `run_aborted` / `run_failed` end the turn. A dropped
   stream reconnects from the last sequence; the run row is re-read if the stream ends early.
4. Re-read the project snapshot and compute `version_from→version_to`, the delta and the digest.
5. Persist the event cursor and run id so `cassette_status` can re-attach after a host timeout.

Runs are durable on the server, so the plugin keeps no job queue and no worker: closing the host
never loses a run.

## Configuration

All settings are environment variables; see the table in the [README](../README.md#configuration).
Defaults target the local Cassette-Editor development stack (API `http://127.0.0.1:8787`, web
`http://127.0.0.1:8080`) without credentials.

Precedence for the project a tool acts on: `project_id` argument → the binding of the working
directory in `state.json` → the most recently used project. `cassette_project` with `action=open`
binds an existing project to the current directory.

## Running the local Cassette stack

In a Cassette-Editor checkout:

```bash
AGENT_AUTH_ENABLED=false bun run dev:lambda
```

This serves the web editor on `:8080`, the API on `:8787` and the worker on `:8788`, and lets the
plugin work without an account. Under this bypass the plugin creates anonymous demo projects
(`try-session-<uuid>`); the editor link is `http://127.0.0.1:8080/try?projectSessionId=<uuid>`.

Video import against the stock backend returns `video_import_requires_backend_update` because the
upload registration only accepts the browser's media processor; the change needed is described in
[v2/backend-changes.md](./v2/backend-changes.md). Audio and image import, runs, history and export
work today.

## Development

```bash
uv sync --group dev
uv run pytest -q                                   # fake backend (tests/fake_cassette), ~10 s
uv run ruff check src tests && uv run ruff format --check src tests
RUN_CASSETTE_LIVE=1 uv run pytest tests/live -q -rs                              # local stack
RUN_CASSETTE_LIVE=1 RUN_CASSETTE_LIVE_EXPORT=1 uv run pytest tests/live -q -rs   # + a real render
uv build && uvx --from dist/*.whl oh-my-cassette --version
```

Run the checkout inside a host:

```bash
claude mcp add cassette-dev -e OH_MY_CASSETTE_LOG=DEBUG -e OH_MY_CASSETTE_HOME=/tmp/omc-dev \
  -- uv run --directory "$PWD" oh-my-cassette
```

`OH_MY_CASSETTE_HOME` keeps a development state file apart from your real one.

### Backend contract

`contracts/` holds JSON captured from the live backend (session state, chat session, snapshot,
command results, run events, media status, import manifest, timeline history). `tests/test_models.py`
validates the pydantic mirrors in `cassette/models.py` against them. Responses are parsed with
`extra="allow"`, so new backend fields never break the plugin; requests are `extra="forbid"`, so a
typo in a request model fails a test instead of the backend. When the backend changes a payload,
recapture the file as described in `contracts/README.md`.

### Adding a tool

Implement it in `tools/`, register it in `server.py` and add the name to `TOOL_NAMES`, return a
dict with a typed `status`, wrap it with `@guarded`, add a fake route and a test, and document it in
`skills/cassette-video-edit/SKILL.md` (then copy the file to `.agents/skills/cassette-video-edit/`).
`tests/test_manifests.py` fails if a tool is undocumented or the two skill copies differ.

## Troubleshooting

| Result | Meaning | What to do |
|---|---|---|
| `error.code = no_project` | No project is bound to this directory and none was used before. | `cassette_project` (`create` or `open <id>`). |
| `cassette_import` item `file_not_found` / `unsupported_type` | Path does not exist, or the extension is not video/audio/image. | Use an absolute path; convert the file. |
| item `video_import_requires_backend_update` | The backend rejected the ffmpeg preparation profile. | Land the P0 change in [v2/backend-changes.md](./v2/backend-changes.md). |
| item `failed` with `readiness` | The backend's processing failed or timed out (`CASSETTE_IMPORT_READY_TIMEOUT_SEC`). | Check the worker log; re-import. |
| `cassette_run` → `needs_input` | The agent asked a question. | Show `question` to the user, then `cassette_answer`. |
| `cassette_run` → `timeout` | The turn outlived `CASSETTE_RUN_TIMEOUT_SEC` or the host's tool timeout. | `cassette_status` re-attaches; raise the host timeout to an hour. |
| `cassette_run` → `running` with `note` | A run was already active on the project. | Wait via `cassette_status`, or `cassette_stop`. |
| `cassette_history` → `rejected` (`at_start`, `at_end`, `target_not_found`) | Nothing to undo/redo, or the group id is unknown. | `cassette_history list`. |
| `error.code = project_timeline_locked` | A run is active; history is read-only meanwhile. | Wait for the run, then retry. |
| `error.code = timeline_empty` on export | The active sequence has no clips. | Edit first. |
| `error.code = contact_sheet_unavailable` | No clip's source media has a local original on this machine. | Import the media from this machine, or skip `contact_sheet`. |
| HTTP 401/403 in `error` | The backend requires an account. | Set `CASSETTE_AUTH_TOKEN` or email/password, or start the stack with `AGENT_AUTH_ENABLED=false`. |
| Connection refused | Nothing listens on `CASSETTE_API_URL`. | Start the stack; check the URL in the host config. |

Server-side logs go to stderr (`OH_MY_CASSETTE_LOG=DEBUG`); hosts show them in their MCP log view.

## Public repository safety

Do not commit `.env` files, tokens or passwords, backend URLs other than the local defaults, media
beyond `tests/fixtures/`, exports, or anything from `~/.oh-my-cassette`.
