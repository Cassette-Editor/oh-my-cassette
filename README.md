<p align="center">
  <img src="assets/banner.jpg" width="80%" alt="Oh My Cassette banner" />
</p>

<h1 align="center">Oh My Cassette</h1>

<p align="center">
  Talk a video into shape from Codex, Claude, Hermes, or the web demo.
</p>

<p align="center">
  <a href="./README.zh-cn.md">简体中文</a> · <b>English</b> ·
  <a href="https://trycassette.online/">Cassette</a> ·
  <a href="https://github.com/Cassette-Editor/oh-my-cassette/releases">Releases</a>
</p>

Oh My Cassette turns local or gateway media plus a natural-language brief into a guided Cassette editing job. Cassette remains the editing engine; this repository provides host integrations, safe media ingestion, job state, background monitoring, completion review, and validated exports.

## Four entry points, one editing core

| Entry point | How it runs | What remains separate |
|---|---|---|
| Codex plugin | `cassette` local stdio MCP child process | No local HTTP server |
| Claude plugin | The same `cassette` local stdio MCP child process | No local HTTP server |
| Hermes plugin | Existing tools, hooks, commands, gateway delivery, and Hermes skill | Keeps `~/.hermes/.env` and gateway roots |
| Web demo | The retained FastAPI upload/chat service and React frontend | Intended only for the demo |

Here, **MCP server** means a process connected to its host over stdin/stdout. It opens no TCP port and does not use the FastAPI demo service. The separate Cassette backend still handles authentication, media processing, agent runs, project state, and rendering; this project does not replace or bundle that backend.

The web demo still needs its server because browsers need upload and application endpoints. Removing that server from the native plugins does not remove the demo, and it does not remove the Cassette backend.

## Requirements

- macOS or Linux;
- Python 3.11–3.13;
- a Cassette account ([sign up](https://trycassette.online/signup/));
- full Cassette API access for the default API transport, or optional Playwright/Chromium for browser transport;
- `ffmpeg` for Hermes gateway normalization and for optional API export thumbnails.

The local launcher creates and updates a locked, plugin-managed virtual environment automatically. Codex and Claude do not need FastAPI, uvicorn, or a separately running service.

## Codex

Add the repository marketplace and install the plugin:

```bash
codex plugin marketplace add https://github.com/Cassette-Editor/oh-my-cassette.git
codex plugin add oh-my-cassette@cassette-editor
```

Start a new Codex task after installation. The plugin contributes one host-neutral video-editing skill and one local MCP server named `cassette`.

## Claude

Add the same repository as a Claude marketplace and install the plugin:

```bash
claude plugin marketplace add Cassette-Editor/oh-my-cassette
claude plugin install oh-my-cassette@cassette-editor
```

Restart Claude Code after installation. `claude plugin details oh-my-cassette@cassette-editor` should report one skill and one MCP server.

## First-run authentication

Authentication is shared by Codex and Claude but isolated from Hermes. Missing credentials never prevent MCP initialization: tools that need Cassette return `auth_required` with the exact private terminal command to run.

From a checkout, the same command is:

```bash
python3 scripts/setup_local_mcp.py
```

It prompts for the password with `getpass`, verifies the credentials against `/api/agent-auth/verify`, and writes only after verification succeeds. Access and refresh tokens stay in memory and are never persisted.

Useful setup variants:

```bash
# Trust media outside the active host project.
python3 scripts/setup_local_mcp.py --allowed-root /absolute/path/to/media

# Explicitly import existing Hermes Cassette credentials once.
python3 scripts/setup_local_mcp.py --import-hermes

# Install pinned Playwright + Chromium and select browser transport.
python3 scripts/setup_local_mcp.py --with-browser
```

Protected config locations:

| Platform | Config | Shared data and exports |
|---|---|---|
| macOS | `~/Library/Application Support/Oh My Cassette/` | `~/Library/Application Support/Oh My Cassette/data/` |
| Linux | `${XDG_CONFIG_HOME:-~/.config}/oh-my-cassette/` | `${XDG_DATA_HOME:-~/.local/share}/oh-my-cassette/` |

Config directories are `0700`; credential files are `0600`. Symlinks and overly permissive credential files are rejected. Credential precedence is process environment, then protected local config. Hermes `.env` import is explicit and never a native-plugin runtime dependency.

If verification shows that the account lacks full API access, setup records that fact and the MCP runtime returns the optional browser setup command. It never silently changes transports.

## Editing from Codex or Claude

Ask the host to edit a file inside the current project, for example:

> Edit `media/trip.mp4` into a 20-second vertical highlight. Add concise English captions and calm instrumental BGM.

The host-neutral skill guides this flow:

1. Ingest each trusted media path. The first call generates a cryptographically random `session_id`.
2. Confirm assets and collect model, thinking level, prompt-optimization, and BGM choices that the user has not already answered.
3. Build the Cassette prompt and start the job in the background.
4. Monitor with bounded 30-second long-polls.
5. Resume a genuine Cassette question with the same `job_id` and the user's response.
6. Review completion. Rendering starts only after an explicit validated `export` decision.
7. Return validated absolute paths, file URIs, MIME type, byte size, and MCP resource links.

The runtime's typed `phase` and `next_action` fields—not prose keywords—control the flow. The default monitoring budget is `CASSETTE_MCP_MONITOR_BUDGET_SEC=1500`. If it expires while a job is still running, the host returns the live job ID instead of losing the job.

### Sessions, restart, and handoff

Codex and Claude share protected auth and data storage, but new sessions are isolated. To deliberately hand work to another host, give it the exact `session_id` and `job_id`; the receiving host should call `cassette_job_status` first.

API jobs persist private LangGraph thread and interrupt metadata, so a user-input-paused job can resume after either host restarts. Browser jobs keep live browser objects only in the MCP process; after restart, resume returns `browser_session_lost` and the user can start a new browser job.

Exports stay under the shared Oh My Cassette data root:

```text
<data-root>/cassette/exports/<job_id>/
```

Only files validated inside that job-specific directory are returned. The MCP runtime never embeds large media bytes and never exposes arbitrary local files.

## MCP tools

The native plugins expose exactly the same 11 tool names as Hermes:

| Tool | Purpose |
|---|---|
| `cassette_ingest_media` | Safely copy trusted project media into an isolated session |
| `cassette_list_assets` | Read the session manifest |
| `cassette_make_prompt` | Build the complete Cassette edit request |
| `cassette_match_bgm` | Match Free To Use BGM |
| `cassette_match_exact_bgm` | Match a concrete title and artist |
| `jamendo_music_matcher` | Match fixed-form Jamendo preferences |
| `cassette_answer_question` | Classify a question or resume a paused job |
| `cassette_run_job` | Start an edit; MCP defaults to background execution |
| `cassette_job_status` | Read status or long-poll for up to 30 seconds |
| `cassette_review_completion` | Validate completion and explicitly approve export |
| `cassette_cancel_job` | Request cooperative cancellation |

All MCP results use a structured envelope with `ok`, typed `data` or `error`, `session_id`, `job_id`, `phase`, `next_action`, and validated artifacts.

## Transports

### API (default)

The API transport talks directly to the Cassette backend: verify auth, upload media, wait for derivatives, run the Cassette LangGraph agent, handle typed interrupts, and render the stored project. It retries authentication once after a `401` and keeps tokens only in memory.

API continuation metadata is private and persisted with the job. A completed edit enters `review_required` unless direct auto-export was explicitly enabled; only `cassette_review_completion(decision="export")` starts rendering.

### Browser (optional)

Browser transport drives the Cassette agent page with pinned Playwright/Chromium. Install it explicitly:

```bash
python3 scripts/setup_local_mcp.py --with-browser
```

It is useful for accounts without full API access and for parity diagnostics. Browser questions can resume while the same MCP process is alive; this is the one intentional restart limitation.

## Hermes

The existing Hermes plugin remains supported with its 11 tools, gateway hooks, commands, delivery notifier, and gateway-specific skill.

```bash
hermes plugins install Cassette-Editor/oh-my-cassette
python3 ~/.hermes/plugins/cassette/scripts/install_plugin.py --setup-only
hermes plugins enable cassette
hermes gateway restart
```

Hermes keeps using `~/.hermes/.env`, `~/.hermes/cassette`, and its QQ/Telegram/Weixin cache roots. Native MCP config does not change those defaults.

Typical gateway flow:

1. Send video, image, or audio assets.
2. Send an edit instruction in the same conversation.
3. Choose model/thinking, prompt optimization, and smart BGM on the first edit.
4. Let the background job deliver progress and the exported video through the stored gateway target.

Useful commands include `/edit`, `/refine`, `/music`, `/cut`, `/check_assets`, `/cassette_model`, `/cassette status <job_id>`, and `/cassette cancel <job_id>`.

Update Hermes with:

```bash
hermes plugins update cassette
hermes gateway restart
```

## Web demo

The FastAPI server is retained **only for the web demo**. It handles browser uploads, chat/session endpoints, frontend assets, and demo deployment behavior. It is independent of the local MCP runtime and still connects to the separate Cassette backend.

Public demo: [http://43.134.224.156:8080/](http://43.134.224.156:8080/)

> [!WARNING]
> The public demo is unauthenticated and intended only for evaluation. Do not upload sensitive, private, regulated, illegal, or restricted content. Uploads and prompts may be processed by the demo operator, Cassette, the configured LLM, and music providers.

Run the demo locally:

```bash
python3 -m venv .venv-web
. .venv-web/bin/activate
pip install -r requirements-web.txt
python -m playwright install chromium
cp deploy/oh-my-cassette-web.env.example ./oh-my-cassette-web.env
# Edit process-environment values, then:
set -a; . ./oh-my-cassette-web.env; set +a
./web_demo/build_frontend.sh
python -m web_demo.server
```

Open `http://127.0.0.1:8080/`. The demo reads only its process environment; it does not use the native protected config or implicitly import Hermes `.env`. Systemd examples remain under `deploy/`.

## Updates

Codex:

```bash
codex plugin marketplace upgrade cassette-editor
codex plugin add oh-my-cassette@cassette-editor
```

Claude:

```bash
claude plugin marketplace update cassette-editor
claude plugin update oh-my-cassette@cassette-editor
```

The launcher hashes `requirements-mcp.lock` and updates its managed environment on the next start. Browser binaries are installed only when requested.

## Diagnostics and troubleshooting

Native plugin diagnostics never print credentials:

```bash
python3 scripts/diagnose_local_mcp.py
```

Hermes diagnostics remain separate:

```bash
python3 scripts/diagnose_install.py
```

Common problems:

- `auth_required`: run the exact setup command from `error.details.setup_command` in a private terminal.
- `api_access_unavailable` or API `forbidden`: run setup with `--with-browser`, or use an account with full API access.
- `source_path_not_allowed`: keep media in the active project or add an absolute trusted root with `--allowed-root`.
- `browser_session_lost`: restart the edit; API transport is required for restart-safe question resume.
- job remains `running`: keep the job ID and check later; do not tight-poll.
- no export artifact: do not invent a path. Inspect structured errors and completion-review state.
- MCP does not appear after install/update: start a new Codex task or restart Claude so plugin discovery runs again.

## Development

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python \
  -r requirements-web.txt -r requirements-browser.lock pytest pytest-xdist
.venv/bin/python -m playwright install chromium

.venv/bin/python -m compileall -q .
.venv/bin/python -m pytest -q -rs -n 4 --dist loadfile
./web_demo/build_frontend.sh
```

Run the real MCP process during development without rebuilding its managed environment:

```bash
CASSETTE_MCP_SKIP_BOOTSTRAP=1 \
CASSETTE_MCP_PYTHON="$PWD/.venv/bin/python" \
.venv/bin/python scripts/run_local_mcp.py
```

Maintainer live acceptance uses credentials only through ephemeral environment variables:

```bash
.venv/bin/python scripts/e2e_local_mcp.py \
  --host codex --transport api \
  --media /absolute/path/to/test.mp4 \
  --instruction "Make a short captioned video."
```

PR CI is deterministic and credential-free. It covers Python 3.11/3.13 on Ubuntu, local MCP bootstrap/protocol smoke on macOS, both native CLI packaging flows, the complete Hermes/web suite, and the frontend build. The manual E2E workflow performs real MCP edits from repository secrets. Live third-party BGM calls are optional diagnostics; deterministic fixtures cover fallback order in PR CI.

See [CONTRIBUTING.md](./CONTRIBUTING.md), [RELEASING.md](./RELEASING.md), [SECURITY.md](./SECURITY.md), and [CHANGELOG.md](./CHANGELOG.md).

## License

MIT. See [LICENSE](./LICENSE).
