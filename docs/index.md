---
title: Oh My Cassette
---

Oh My Cassette connects your coding agent — [Claude Code](https://claude.com/claude-code),
[Codex](https://github.com/openai/codex), [OpenCode](https://opencode.ai) or
[Hermes Agent](https://github.com/nousresearch/hermes-agent) — to the Cassette editing agent. It is a
local stdio MCP server that relays to the Cassette MCP service: your agent passes your words through,
Cassette edits, and the finished video lands in `cassette-exports/`.

## Install

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

Then sign in once with `uvx oh-my-cassette==0.6.0 login`. Requires [uv](https://docs.astral.sh/uv/),
`ffmpeg` for video, and an invited [Cassette](https://trycassette.online) account. Other hosts and
every option are in the [README](https://github.com/Cassette-Editor/oh-my-cassette#readme).

## Documentation

- [README](https://github.com/Cassette-Editor/oh-my-cassette#readme) — install, configuration, how a turn looks
- [中文说明](https://github.com/Cassette-Editor/oh-my-cassette/blob/main/README.zh-cn.md)
- [Changelog](https://github.com/Cassette-Editor/oh-my-cassette/blob/main/CHANGELOG.md)
- [Security](https://github.com/Cassette-Editor/oh-my-cassette/blob/main/SECURITY.md) — trust boundaries and reporting
- [Privacy](privacy.md) — what leaves your machine, what stays on it, and how to remove it
- [Terms of use](terms.md) — the MIT licence in plain language, and where this plugin ends and Cassette begins
