<p align="center">
  <img src="./assets/logo-400.png" alt="Oh My Cassette" width="120" />
</p>

<h1 align="center">Oh My Cassette</h1>

<p align="center">
  Video editing through the <a href="https://trycassette.online">Cassette</a> agent, from Claude Code, Codex, OpenCode and Hermes.<br />
  <sub>An MCP server on PyPI. One project = one editor link = one running conversation with the editing agent.</sub>
</p>

<p align="center">
  <a href="https://pypi.org/project/oh-my-cassette/"><img alt="PyPI" src="https://img.shields.io/pypi/v/oh-my-cassette?label=oh-my-cassette" /></a>
  <a href="./LICENSE"><img alt="MIT" src="https://img.shields.io/badge/license-MIT-blue" /></a>
  <a href="./README.zh-cn.md">中文</a>
</p>

---

## Install in 30 seconds

You need [uv](https://docs.astral.sh/uv/) (it fetches Python and the package on first start) and `ffmpeg` on PATH (video import and contact sheets).

```bash
# Claude Code
claude plugin marketplace add Cassette-Editor/oh-my-cassette
claude plugin install oh-my-cassette@cassette-editor
```

```bash
# Codex
codex plugin marketplace add https://github.com/Cassette-Editor/oh-my-cassette.git
codex plugin add oh-my-cassette@cassette-editor
```

```bash
# Hermes
hermes mcp add cassette --command uvx --args oh-my-cassette==0.5.0
```

OpenCode and any other MCP host: see [Install](#install) below. Then start the Cassette stack (or point the plugin at a deployed one), restart your agent, and say:

> *Import ./footage/*.mp4 and cut a 30-second travel vlog with a title at the start.*

## Overview

Oh My Cassette turns an editing conversation in your coding agent into edits on a real Cassette project:

| You say | The agent calls | What happens |
| --- | --- | --- |
| "Start a new video project" | `cassette_project` | Creates a project, binds it to the current directory, returns an `editor_url` you can open in a browser to watch live. |
| "Use clip1.mp4, clip2.mov and music.mp3" | `cassette_import` | Hashes, transcodes (video, locally with ffmpeg), uploads through presigned URLs and waits until the backend can use the media. |
| "Cut a 30-second vlog with a title" | `cassette_run` | Sends your words verbatim to the Cassette editing agent as one turn; streams progress; returns the agent's answer, `vN→vM`, a bounded timeline digest. |
| (the agent asks a question) | `cassette_answer` | Answers the pending question and continues the same run. |
| "Undo that" | `cassette_history` | Moves the project's history cursor (undo / redo / restore before a commit). |
| "What's on the timeline?" | `cassette_timeline` | Version, per-track digest, media library, optional local contact sheet. |
| "Export it" | `cassette_export` | Renders on the backend and downloads the MP4 into `./exports`. |
| (host timeout) | `cassette_status` / `cassette_stop` | Re-attaches to the running turn, or stops it (committed edits stay). |

Every tool returns a typed `status` so the host routes on data, not prose. Nothing renders during an edit: the backend keeps the timeline as a versioned document, and the plugin reads it back after each turn.

### Case videos

Six real cases edited end to end through Oh My Cassette, each with the exact prompt, inputs and processing time: [docs/showcase.md](./docs/showcase.md).

## Requirements

- A Cassette-Editor backend on the current `main` line (the in-process agent runtime under `/api/agent/*`). Locally: `AGENT_AUTH_ENABLED=false bun run dev:lambda` in the Cassette-Editor checkout.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) 0.5 or newer. `uvx oh-my-cassette==0.5.0` resolves Python 3.11–3.13 by itself.
- `ffmpeg` and `ffprobe` on PATH for video import and contact sheets (audio and image import work without them).

Supported imports: video `mp4`, `mov`; audio `mp3`, `wav`, `m4a`, `aac`, `ogg`, `oga`, `opus`, `flac`; image `jpg`, `jpeg`, `png`, `gif`, `webp`, `bmp`, `avif`.

## Configuration

Everything is an environment variable, so all four hosts configure the server the same way.

| Variable | Default | Meaning |
| --- | --- | --- |
| `CASSETTE_API_URL` | `http://127.0.0.1:8787` | Backend (render server) base URL. |
| `CASSETTE_WEB_URL` | `http://127.0.0.1:8080` (or the API URL when it is not local) | Web editor base URL used for `editor_url`. |
| `CASSETTE_AUTH_TOKEN` | unset | Bearer token for a deployed backend. Unset = unauthenticated (local `AGENT_AUTH_ENABLED=false` stack). |
| `CASSETTE_EMAIL` / `CASSETTE_PASSWORD` | unset | Alternative to a token: exchanged once through `/api/agent-auth/verify`, cached in `credentials.json` (0600). |
| `OH_MY_CASSETTE_HOME` | `~/.oh-my-cassette` | Local state: project bindings, media map, event cursors, contact sheets. |
| `CASSETTE_MODEL` / `CASSETTE_REASONING_EFFORT` | backend defaults | Default model (`luna`, `terra`, `sol`) and effort (`none`…`max`) for runs. |
| `CASSETTE_RUN_TIMEOUT_SEC` | `3300` | How long one `cassette_run` call follows a run before returning `timeout` (re-attach with `cassette_status`). |
| `CASSETTE_IMPORT_READY_TIMEOUT_SEC` / `CASSETTE_EXPORT_TIMEOUT_SEC` | `900` / `1800` | Readiness and render wait limits. |
| `CASSETTE_FFMPEG` / `CASSETTE_FFPROBE` | `ffmpeg` / `ffprobe` | Binaries to use. |
| `OH_MY_CASSETTE_LOG` | `WARNING` | Log level on stderr. |

Without credentials the plugin creates **anonymous projects** (`try-session-<uuid>`, editor link `/try?projectSessionId=…`). With a token or email/password it creates **owned projects** (`/editor/p/<uuid>`).

## Install

### Claude Code

Plugin (recommended): the two commands at the top. Claude asks for `api_url`, `web_url` and an optional `auth_token` on install (`userConfig`), and starts `uvx oh-my-cassette==0.5.0` with a 60-minute tool timeout.

Project scope without the plugin: copy [`.mcp.json`](./.mcp.json) into your project and replace the `${user_config.*}` values, or run:

```bash
claude mcp add --transport stdio cassette -e CASSETTE_API_URL=http://127.0.0.1:8787 -- uvx oh-my-cassette==0.5.0
```

The skill lives in [`skills/cassette-video-edit/SKILL.md`](./skills/cassette-video-edit/SKILL.md) and ships with the plugin.

### Codex

Plugin: the two commands at the top ([`.codex-plugin/plugin.json`](./.codex-plugin/plugin.json) declares the server inline with `tool_timeout_sec: 3600` and passes the `CASSETTE_*` variables through from your shell). Or add the server directly:

```bash
codex mcp add cassette --env CASSETTE_API_URL=http://127.0.0.1:8787 -- uvx oh-my-cassette==0.5.0
```

### OpenCode

Add to `opencode.json` (project or `~/.config/opencode/opencode.json`):

```json
{
  "mcp": {
    "cassette": {
      "type": "local",
      "command": ["uvx", "oh-my-cassette==0.5.0"],
      "environment": { "CASSETTE_API_URL": "http://127.0.0.1:8787", "CASSETTE_WEB_URL": "http://127.0.0.1:8080" },
      "timeout": 3600000
    }
  }
}
```

Then copy the skill so OpenCode loads it:

```bash
mkdir -p ~/.config/opencode/skills/cassette-video-edit
curl -fsSL https://raw.githubusercontent.com/Cassette-Editor/oh-my-cassette/main/skills/cassette-video-edit/SKILL.md \
  -o ~/.config/opencode/skills/cassette-video-edit/SKILL.md
```

OpenCode has no MCP elicitation, which is fine: questions come back as `status=needs_input` and are answered with `cassette_answer`.

### Hermes

```bash
hermes mcp add cassette --command uvx --args oh-my-cassette==0.5.0 --env CASSETTE_API_URL=http://127.0.0.1:8787
mkdir -p ~/.hermes/skills/cassette-video-edit
curl -fsSL https://raw.githubusercontent.com/Cassette-Editor/oh-my-cassette/main/skills/cassette-video-edit/SKILL.md \
  -o ~/.hermes/skills/cassette-video-edit/SKILL.md
```

Set `mcp_servers.cassette.timeout: 3600` in `~/.hermes/config.yaml` so long edits are not cut off. Hermes is a plain MCP host here: the 0.4 gateway/plugin layer is gone.

### Any other MCP host

Command `uvx`, args `["oh-my-cassette==0.5.0"]`, stdio transport, a tool timeout of at least an hour, and the environment variables above. No `pipx`? `pipx run oh-my-cassette==0.5.0` works too.

## How a turn looks

```text
you:    Import intro.mp4 and beach.mov, then make a 20-second cut with the title "Kota Kinabalu".
agent:  cassette_project → editor_url http://127.0.0.1:8080/try?projectSessionId=…
        cassette_import  → 2 ready
        cassette_run     → status completed, v0→v3: added 4 clips; tracks added Title Overlay …
        "Done. Added a 20 s cut from both clips with the title at the start (v0→v3). Open it: <editor_url>"
you:    Make the title last 2 seconds longer.
agent:  cassette_run     → status completed, v3→v4: changed clip_title
```

Open `editor_url` in a browser at any time: it subscribes to the same chat session, so you see the run as it happens and can take over by hand.

## Update

Bump the pin in your host config (`oh-my-cassette==<new version>`) or reinstall the plugin; `uvx` fetches the new wheel on next start. Releases: [CHANGELOG.md](./CHANGELOG.md).

## Development

```bash
git clone https://github.com/Cassette-Editor/oh-my-cassette && cd oh-my-cassette
uv sync --group dev
uv run pytest -q                                   # fake backend, no network
RUN_CASSETTE_LIVE=1 uv run pytest tests/live -q    # against a local Cassette-Editor stack
uv run oh-my-cassette                              # the server itself (stdio)
```

See [docs/development.md](./docs/development.md) for the architecture and [RELEASING.md](./RELEASING.md) for the PyPI release flow.

## License

MIT. Oh My Cassette is the client; the Cassette service has its own terms.

<sub>MCP registry: `mcp-name: io.github.Cassette-Editor/oh-my-cassette`</sub>
