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

It reports runtime bootstrap, protected config, transport, project/media roots, and host-neutral data paths without printing credentials. Common MCP errors are actionable: `auth_required` includes the private setup command, `source_path_not_allowed` identifies the trusted-root problem, and `browser_session_lost` explains when a browser job cannot survive a restart.

For Hermes, run:

```bash
python3 scripts/diagnose_install.py
```

The diagnostic checks:

- plugin install path (symlink installs and `hermes plugins install` git clones are both recognized);
- whether the plugin is enabled in Hermes;
- `~/.hermes/.env` values, with secrets redacted;
- `ffmpeg` and `ffprobe`;
- Playwright in the Hermes Python environment;
- Cassette URL reachability;
- Cassette login credentials by opening the Agent page in Chromium;
- Hermes gateway status.

If incoming media fails with `transcoder_missing`, run the installer again so it records explicit `CASSETTE_FFMPEG_BIN` and `CASSETTE_FFPROBE_BIN` paths:

```bash
python3 scripts/install_plugin.py \
  --skip-plugin-enable \
  --skip-cassette-url \
  --skip-cassette-auth \
  --skip-jamendo-auth \
  --skip-playwright-install
```

## Configuration

Codex and Claude share the platform-standard Oh My Cassette config and data directories. Their credentials and job state are separate from Hermes. The active host project is trusted automatically; add any other media directory explicitly with `setup_local_mcp.py --allowed-root`. The web demo reads only its process environment and does not use either plugin's stored credentials.

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
JAMENDO_CLIENT_SECRET=your_client_secret
```

`JAMENDO_CLIENT_SECRET` is reserved for future use. It is not sent to Jamendo and is not written to job metadata.

Transparency and direct-edit flags (API transport):

| Flag | Default | Effect |
|---|---|---|
| `CASSETTE_API_STREAM` | on | SSE run-event listener feeding `timeline_delta`/`plan_progress`; `0` = pure polling |
| `CASSETTE_PLAN_REVIEW` | `user` on MCP, `auto` on gateway | surface `edit_plan_review` as a question vs auto-approve |
| `CASSETTE_UNATTENDED` | off | `1` restores fully headless auto-approve semantics |
| `CASSETTE_DIRECT_EDIT` | off | enable the `cassette_edit` surgical no-LLM lane |
| `CASSETTE_AUTH_TOKEN` | unset | pre-issued bearer, skips `/api/agent-auth/verify` (local dev/CI) |
| `CASSETTE_WEB_URL` | origin of `CASSETTE_URL` | web origin used for `editor_url` deep links |




## Development
Create a local test environment:

```bash
uv venv .venv
uv pip install --python .venv/bin/python pytest playwright
.venv/bin/python -m playwright install chromium
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

The deterministic test suite covers core parity, all 11 tools, real stdio protocol calls, long-polling, restart/resume behavior, state transitions, resource links, auth and filesystem security, both plugin manifests, the existing Hermes/web suite, and frontend builds. Maintainer-triggered live E2E uses repository secrets through ephemeral environment variables; PR CI stays credential-free.

Run the local Cassette E2E harness:

```bash
.venv/bin/python scripts/e2e_local_cassette.py \
  --media tests/fixtures/sample.mp4 \
  --instruction "Make a short captioned video under 10 seconds."
```

#### Cassette transport

The plugin can reach Cassette two ways, selected by `CASSETTE_TRANSPORT`, and **both are fully supported** — pick whichever fits your deployment:

- **`api` (default)** — calls the Cassette server APIs directly (auth → media upload → LangGraph agent run → render-from-stored-project export), no browser. This is the default because it avoids the reliability weakness of DOM scraping and needs no Playwright/Chromium. It reuses your existing `CASSETTE_AUTH_EMAIL`/`CASSETTE_AUTH_PASSWORD`; the API origin defaults to the deployed Cassette (override with `CASSETTE_API_URL` only for self-hosted). **Requirement:** the account must have full API access (for `/api/projects` and `/api/export`) — a `403`/`forbidden` error reported by the transport indicates it does not, in which case set `CASSETTE_TRANSPORT=browser`.
- **`browser`** — drives the Cassette web UI with Playwright (the original, battle-tested path). Set `CASSETTE_TRANSPORT=browser` to use it. Requires Playwright/Chromium installed. Behavior is byte-identical to the pre-transport-seam plugin.

Switching is a single env var and nothing downstream changes: both transports return the identical job-result dict, so notifications, delivery, and reporting are the same either way.

Uploaded media is linked to the agent run by session id (the upload `x-session-id` equals the run's `mediaSessionId`), and the run carries the same full session/project/run context the editor sends. Before starting the run, the transport waits for uploaded media to be fully processed — analysis evidence/embeddings (which the agent reads) and the render-source derivative (which the export needs) — so it never commits an empty edit or hits an "render-source is missing" export (tunable via `CASSETTE_API_MEDIA_READY_TIMEOUT_SEC`). Cancellation (`/cut`) is honored mid-run, agent timeouts report `timed_out`, and a run whose queue never starts fails fast as `agent_run_not_started` (tunable via `CASSETTE_API_RUN_START_TIMEOUT_SEC`) instead of hanging until the job timeout. The transport requires the Cassette backend's LangGraph run queue to be draining runs and its media render-source pipeline to be healthy.

The API path aims for behavioral parity with the browser path: it honors the user's model choice (mapping the UI label to a model id, and failing loudly under `CASSETTE_REQUIRE_MODEL_SELECTION` if unmappable) and `CASSETTE_DEFAULT_THINKING_LEVEL`; sends the model-selection notice; records live stage progress (`current_stage`, `stage_timings`, `progress_events`) and delivers a periodic **text** progress heartbeat (there is no browser to screenshot); classifies Cassette questions so routine ones auto-continue while genuine choices/missing-assets return `needs_user`; dedupes uploads across a reused session; and, like the browser path, routes a completed edit through the Hermes supervisor completion review (`cassette_review_completion`) before exporting — set `CASSETTE_API_AUTO_EXPORT=1` to export directly on agent success instead. The one irreducible difference is `final_screenshot`: with no browser, the transport substitutes a still frame extracted from the exported mp4 (`CASSETTE_API_EXPORT_THUMBNAIL`).

The same E2E flow can run on either transport, and a parity harness diffs the two outcomes (terminal status, deliverable-output count, error-code set):

```bash
# single transport
.venv/bin/python scripts/e2e_local_cassette.py --transport api \
  --media tests/fixtures/sample.mp4 --instruction "Make a short captioned video."

# browser-vs-api parity (requires a full-access account; the render-from-project export endpoint
# must be live on the Cassette server)
.venv/bin/python scripts/e2e_transport_parity.py \
  --media tests/fixtures/sample.mp4 --instruction "Make a short captioned video."
```

Run the web demo service:

```bash
uv venv .venv-web
uv pip install --python .venv-web/bin/python -r requirements-web.txt
.venv-web/bin/python -m playwright install chromium
# Build the browser UI (Vite/React -> web_demo/frontend/dist); requires Node.js + npm.
./web_demo/build_frontend.sh
set -a
. ./oh-my-cassette-web.env
set +a
.venv-web/bin/python -m web_demo.server
```

The browser UI is a Vite + React + TypeScript app under `web_demo/frontend`; `web_demo/build_frontend.sh` compiles it to `web_demo/frontend/dist`, which the server serves under `/static`. The build output is not committed, so build it on each deploy (and re-run it after pulling frontend changes). For live frontend iteration, `cd web_demo/frontend && npm run dev` runs Vite with `/api` proxied to `http://127.0.0.1:8088`, so run the FastAPI server alongside it.

The web demo reads `CASSETTE_*`, `DEEPSEEK_*`, and `OMC_WEB_*` from process environment variables. Users can also enter a temporary DeepSeek API key in the browser settings; it is sent only with requests to this server and is not written to the repository or server disk. A systemd template lives at `deploy/oh-my-cassette-web.service.example`, with an environment template at `deploy/oh-my-cassette-web.env.example`.

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

