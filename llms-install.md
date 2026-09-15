# Installing Oh My Cassette (for AI agents)

Oh My Cassette is a stdio MCP server published on PyPI as `oh-my-cassette`. Hosts start it with
`uvx oh-my-cassette==0.5.0`; there is nothing to clone or build.

## Prerequisites

- `uv` on PATH (https://docs.astral.sh/uv/getting-started/installation/). It provides Python 3.11–3.13.
- `ffmpeg` and `ffprobe` on PATH for video import and contact sheets.
- A reachable Cassette-Editor backend. Local default: `AGENT_AUTH_ENABLED=false bun run dev:lambda`
  in the Cassette-Editor checkout → API `http://127.0.0.1:8787`, web `http://127.0.0.1:8080`.

## Claude Code

```bash
claude plugin marketplace add Cassette-Editor/oh-my-cassette
claude plugin install oh-my-cassette@cassette-editor
```

Or project scope: `claude mcp add --transport stdio cassette -e CASSETTE_API_URL=http://127.0.0.1:8787 -- uvx oh-my-cassette==0.5.0`.
Health check: `claude mcp list` shows `cassette: … - ✓ Connected`.

## Codex

```bash
codex plugin marketplace add https://github.com/Cassette-Editor/oh-my-cassette.git
codex plugin add oh-my-cassette@cassette-editor
```

Or: `codex mcp add cassette --env CASSETTE_API_URL=http://127.0.0.1:8787 -- uvx oh-my-cassette==0.5.0`.

## OpenCode

Add the `mcp.cassette` block from `opencode.json` in this repository to the user's `opencode.json`,
then copy `skills/cassette-video-edit/SKILL.md` to `~/.config/opencode/skills/cassette-video-edit/SKILL.md`.

## Hermes

```bash
hermes mcp add cassette --command uvx --args oh-my-cassette==0.5.0 --env CASSETTE_API_URL=http://127.0.0.1:8787
```

Copy `skills/cassette-video-edit/SKILL.md` to `~/.hermes/skills/cassette-video-edit/SKILL.md` and set
`mcp_servers.cassette.timeout: 3600` in `~/.hermes/config.yaml`.

## Environment variables

`CASSETTE_API_URL`, `CASSETTE_WEB_URL`, `CASSETTE_AUTH_TOKEN` (or `CASSETTE_EMAIL` + `CASSETTE_PASSWORD`),
`OH_MY_CASSETTE_HOME` (default `~/.oh-my-cassette`), `CASSETTE_MODEL`, `CASSETTE_REASONING_EFFORT`,
`CASSETTE_RUN_TIMEOUT_SEC`, `CASSETTE_IMPORT_READY_TIMEOUT_SEC`, `CASSETTE_EXPORT_TIMEOUT_SEC`,
`CASSETTE_FFMPEG`, `CASSETTE_FFPROBE`, `OH_MY_CASSETTE_LOG`. See README.md for defaults.

## Verify

`uvx oh-my-cassette==0.5.0 --version` prints `oh-my-cassette 0.5.0`. In the host, call
`cassette_project` with `action=create`: it returns `status=ok`, a `project_id` and an `editor_url`.

## Uninstalling

Remove the server from the host config (`claude plugin uninstall oh-my-cassette@cassette-editor`,
`codex plugin remove oh-my-cassette`, delete the `opencode.json` block, `hermes mcp remove cassette`),
then delete `~/.oh-my-cassette` (local state and cached credentials) and `uv cache clean oh-my-cassette`.
