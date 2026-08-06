# Development & Troubleshooting

[← Back to README](../README.md)


> [!TIP]
> Join our Discord community to connect with contributors and fellow `oh-my-cassette` users.
>
> [![Discord](https://img.shields.io/discord/1514649803626250452?style=for-the-badge&logo=discord&logoColor=white&label=Discord&labelColor=black&color=5865F2)](https://discord.gg/qd9NY4k8d7)

## Common Q&A

### 1. Why does my Hermes Agent fail to respond on QQ or Telegram?

Please check your Hermes Agent model setup, network connectivity, and API connectivity. You can also restart the gateway:

```bash
hermes gateway stop
hermes gateway restart
```

### 2. Why is there a network issue when connecting to Cassette?

Please check if you can access https://sg.trycassette.online/agent or https://trycassette.online/agent. If not, you may check your network settings to open Cassette.

### 3. Why is the runtime slow?

The editing process depends on Hermes Agent API latency and Cassette service load. An edit task may take approximately 5–20 minutes depending on the complexity of the task, the selected model, and the thinking level.

If Hermes or Cassette gets stuck, first send `/cut` to stop the current Cassette edit, then send `/stop` to stop Hermes. After that, try again in the same session.


## Diagnose

For the Codex and Claude local MCP plugin, run:

```bash
python3 scripts/diagnose_local_mcp.py
```

It reports runtime bootstrap, protected config, project/media roots, and host-neutral data paths without printing credentials. Common MCP errors are actionable: `auth_required` includes the private setup command and `source_path_not_allowed` identifies the trusted-root problem.

For Hermes, run:

```bash
python3 scripts/diagnose_install.py
```

The diagnostic checks:

- plugin install path (symlink installs and `hermes plugins install` git clones are both recognized);
- whether the plugin is enabled in Hermes;
- `~/.hermes/.env` values, with secrets redacted;
- `ffmpeg` and `ffprobe`;
- Cassette URL reachability;
- Cassette login credentials against the agent-auth API;
- Hermes gateway status.

If incoming media fails with `transcoder_missing`, run the installer again so it records explicit `CASSETTE_FFMPEG_BIN` and `CASSETTE_FFPROBE_BIN` paths:

```bash
python3 scripts/install_plugin.py \
  --skip-plugin-enable \
  --skip-cassette-url \
  --skip-cassette-auth \
  --skip-jamendo-auth
```

## Configuration

Codex and Claude share the platform-standard Oh My Cassette config and data directories. Their credentials and job state are separate from Hermes. The active host project is trusted automatically; add any other media directory explicitly with `setup_local_mcp.py --allowed-root`.

The installer writes normal runtime settings to `~/.hermes/.env`. You can also edit that file manually.
Minimum useful values:

```bash
CASSETTE_URL=https://sg.trycassette.online/agent
CASSETTE_AUTH_EMAIL=you@example.com
CASSETTE_AUTH_PASSWORD=your-generated-cassette-password
CASSETTE_ASSET_ROOT=$HOME/.hermes/cassette
CASSETTE_HEADLESS=true
CASSETTE_FORCE_H264=true
```

Default media source roots:

```text
~/.hermes/qqbot
~/.hermes/telegram
~/.hermes/weixin
~/.hermes/cache
~/.hermes/tmp
```

If your gateway stores media elsewhere:

```bash
CASSETTE_ALLOWED_SOURCE_ROOTS="$HOME/.hermes/qqbot:$HOME/.hermes/telegram:$HOME/.hermes/cache:$HOME/.hermes/tmp:/path/to/media"
```

Optional Jamendo smart BGM configuration:

```bash
JAMENDO_CLIENT_ID=your_client_id
```

Create a read-only application in the [Jamendo developer portal](https://devportal.jamendo.com/) and copy its Client ID. The read-only music flow does not use or request a Client Secret. BYOK assigns API access and quota to the user's Jamendo application; it does not grant commercial rights to selected music. Review every track's license and attribution requirements before publishing.

Transparency and direct-edit flags (API transport):

| Flag | Default | Effect |
|---|---|---|
| `CASSETTE_API_STREAM` | on | SSE run-event listener feeding `timeline_delta`/`plan_progress`; `0` = pure polling |
| `CASSETTE_PLAN_REVIEW` | `user` on MCP, `auto` on gateway | surface `edit_plan_review` as a question vs auto-approve |
| `CASSETTE_UNATTENDED` | off | `1` restores fully headless auto-approve semantics |
| `CASSETTE_DIRECT_EDIT` | off | enable the `cassette_edit` surgical no-LLM lane |
| `CASSETTE_AUTH_TOKEN` | unset | pre-issued bearer, skips `/api/agent-auth/verify` (local dev/CI) |




## Development
Create a local test environment:

```bash
uv venv .venv
uv pip install --python .venv/bin/python pytest
```

Run checks:

```bash
python3 -m compileall -q .
.venv/bin/python -m pytest -q
```

Run the real stdio MCP process against the development environment:

```bash
CASSETTE_MCP_SKIP_BOOTSTRAP=1 \
CASSETTE_MCP_PYTHON="$PWD/.venv/bin/python" \
.venv/bin/python scripts/run_local_mcp.py
```

The deterministic test suite covers core parity, all 11 tools, real stdio protocol calls, long-polling, restart/resume behavior, state transitions, resource links, auth and filesystem security, both plugin manifests, and the existing Hermes suite. Maintainer-triggered live E2E uses repository secrets through ephemeral environment variables; PR CI stays credential-free.

Run the local Cassette E2E harness:

```bash
.venv/bin/python scripts/e2e_local_cassette.py \
  --media tests/fixtures/sample.mp4 \
  --instruction "Make a short captioned video under 10 seconds."
```

#### Cassette transport

There is one transport: direct calls to the Cassette server APIs (auth → media upload → LangGraph agent run → render-from-stored-project export). It reuses your existing `CASSETTE_AUTH_EMAIL`/`CASSETTE_AUTH_PASSWORD`; the API origin defaults to the deployed Cassette (override with `CASSETTE_API_URL` only for self-hosted).

A Playwright transport used to sit behind `CASSETTE_TRANSPORT=browser`, kept on the theory that the web UI could reach endpoints the API could not. It could not — the server authorizes by endpoint, and the editor's export button posts to the same endpoints this transport calls — so it was removed along with its Chromium dependency and its restart-fragile session model. `CASSETTE_TRANSPORT` no longer selects anything; a leftover `browser` value is reported once on stderr and ignored.

Every endpoint the plugin calls, export included, is an agent operation. The plugin never inspects a Cassette access level, so a `403` is relayed as a server-side refusal to report upstream, not as something to reconfigure here.

Uploaded media is linked to the agent run by session id (the upload `x-session-id` equals the run's `mediaSessionId`), and the run carries the same full session/project/run context the editor sends. Before starting the run, the transport waits for uploaded media to be fully processed — analysis evidence/embeddings (which the agent reads) and the render-source derivative (which the export needs) — so it never commits an empty edit or hits an "render-source is missing" export (tunable via `CASSETTE_API_MEDIA_READY_TIMEOUT_SEC`). Cancellation (`/cut`) is honored mid-run, agent timeouts report `timed_out`, and a run whose queue never starts fails fast as `agent_run_not_started` (tunable via `CASSETTE_API_RUN_START_TIMEOUT_SEC`) instead of hanging until the job timeout. The transport requires the Cassette backend's LangGraph run queue to be draining runs and its media render-source pipeline to be healthy.

The transport honors the user's model choice (mapping the UI label to a model id, and failing loudly under `CASSETTE_REQUIRE_MODEL_SELECTION` if unmappable) and `CASSETTE_DEFAULT_THINKING_LEVEL`; sends the model-selection notice; records live stage progress (`current_stage`, `stage_timings`, `progress_events`) and delivers a periodic **text** progress heartbeat; classifies Cassette questions so routine ones auto-continue while genuine choices/missing-assets return `needs_user`; dedupes uploads across a reused session; and routes a completed edit through the Hermes supervisor completion review (`cassette_review_completion`) before exporting — set `CASSETTE_API_AUTO_EXPORT=1` to export directly on agent success instead. `final_screenshot` is a still frame extracted from the exported mp4 (`CASSETTE_API_EXPORT_THUMBNAIL`).

The end-to-end flow against a real Cassette account:

```bash
.venv/bin/python scripts/e2e_local_cassette.py \
  --media tests/fixtures/sample.mp4 --instruction "Make a short captioned video."
```

The browser-based web demo is no longer part of this repository. It lives in [oh-my-cassette-web](https://github.com/Cassette-Editor/oh-my-cassette-web), with its own FastAPI service, Vite/React UI, and deployment templates. It runs on a shared Cassette account through a Playwright transport, which is why it could not follow this repository onto the agent-account API path.

Real gateway E2E tests are opt-in only and are skipped by default:

```bash
RUN_CASSETTE_E2E=1 .venv/bin/python -m pytest -q -m e2e
```

## Public Repository Safety

Do not commit:

- `.env` or `.env.e2e`;
- real gateway tokens, account IDs, chat IDs, or raw `wxid` values;
- Cassette credentials;
- Jamendo credentials;
- downloaded media, exports, job state, browser traces, or local runtime cache.

Hermes runtime state belongs under `~/.hermes/cassette`; Codex and Claude runtime state belongs under the platform-standard Oh My Cassette data directory. Neither belongs in this repository.
