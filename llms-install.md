# Installing Oh My Cassette (for AI agents)

Oh My Cassette is a stdio MCP bridge published on PyPI as `oh-my-cassette`. Hosts start it with
`uvx oh-my-cassette==0.5.0`; there is nothing to clone or build. It relays to the Cassette MCP
service at `CASSETTE_MCP_URL`, so the editing tools come from that service.

## Prerequisites

- `uv` on PATH (https://docs.astral.sh/uv/getting-started/installation/). It provides Python 3.11–3.13.
- `ffmpeg` and `ffprobe` on PATH to import video (`brew install ffmpeg`, `sudo apt install ffmpeg`,
  `winget install ffmpeg`), or `CASSETTE_FFMPEG` / `CASSETTE_FFPROBE` set to them.
- A reachable Cassette MCP service (contract v1) and an access token for it. Check it with
  `uvx oh-my-cassette==0.5.0 check --url http://127.0.0.1:8790/mcp --token <token>`: every line should be PASS.

## Claude Code

```bash
claude plugin marketplace add Cassette-Editor/oh-my-cassette
claude plugin install oh-my-cassette@cassette-editor
```

The plugin asks for `mcp_url` and `auth_token`. Or project scope:
`claude mcp add --transport stdio cassette -e CASSETTE_MCP_URL=http://127.0.0.1:8790/mcp -e CASSETTE_AUTH_TOKEN=<token> -- uvx oh-my-cassette==0.5.0`.
Health check: `claude mcp list` shows `cassette: … - ✓ Connected`.

## Codex

```bash
codex plugin marketplace add https://github.com/Cassette-Editor/oh-my-cassette.git
codex plugin add oh-my-cassette@cassette-editor
```

The plugin passes `CASSETTE_*` variables through from the shell. Or:
`codex mcp add cassette --env CASSETTE_MCP_URL=http://127.0.0.1:8790/mcp --env CASSETTE_AUTH_TOKEN=<token> -- uvx oh-my-cassette==0.5.0`.

## OpenCode

Add the `mcp.cassette` block from `opencode.json` in this repository to the user's `opencode.json`,
then copy `skills/cassette-video-edit/SKILL.md` to `~/.config/opencode/skills/cassette-video-edit/SKILL.md`.

## Hermes

```bash
hermes mcp add cassette --command uvx --args oh-my-cassette==0.5.0 \
  --env CASSETTE_MCP_URL=http://127.0.0.1:8790/mcp --env CASSETTE_AUTH_TOKEN=<token>
```

Copy `skills/cassette-video-edit/SKILL.md` to `~/.hermes/skills/cassette-video-edit/SKILL.md` and set
`mcp_servers.cassette.timeout: 600` in `~/.hermes/config.yaml`.

## Tool timeout

Configure 600 seconds (600000 ms where the host counts milliseconds). Every call stays within it:
the bridge waits at most `CASSETTE_LOCAL_WAIT_SEC` (240) for local preparation and the service waits a
bounded time for its own work; longer work answers `preparing`, `running` or `processing` and continues
on the next call.

## Environment variables

`CASSETTE_MCP_URL` (default `http://127.0.0.1:8790/mcp`), `CASSETTE_AUTH_TOKEN` (required by the
service), `CASSETTE_WORKSPACE` (default: the server's working directory), `CASSETTE_ALLOWED_ROOTS`,
`CASSETTE_DOWNLOAD_DIR`, `CASSETTE_MAX_UPLOAD_MB`, `CASSETTE_MAX_DOWNLOAD_MB`, `CASSETTE_UPLOAD_ANY_TYPE`,
`CASSETTE_LOCAL_WAIT_SEC`, `CASSETTE_FFMPEG`, `CASSETTE_FFPROBE`, `CASSETTE_TEMP_DIR`,
`CASSETTE_CONNECT_TIMEOUT_SEC`, `OH_MY_CASSETTE_LOG`. See README.md for defaults.

## Verify

`uvx oh-my-cassette==0.5.0 --version` prints `oh-my-cassette 0.5.0`. In the host, the tool list should
show the service's tools. If it shows only `cassette_bridge_status`, call it: it reports the endpoint
URL, an `error_code` (`bridge.backend_unreachable`, `bridge.unauthorized` or `bridge.upgrade_required`)
and the reason.

## Uninstalling

Remove the server from the host config (`claude plugin uninstall oh-my-cassette@cassette-editor`,
`codex plugin remove oh-my-cassette`, delete the `opencode.json` block, `hermes mcp remove cassette`),
then run `uv cache clean oh-my-cassette`. The bridge keeps no local state; exported files stay in
`cassette-exports/`.
