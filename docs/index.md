---
title: Oh My Cassette
---

Oh My Cassette is an open-source AI video editing plugin and local MCP server for
[Claude Code](https://claude.com/claude-code), [Codex](https://github.com/openai/codex),
[Hermes Agent](https://github.com/nousresearch/hermes-agent), and [OpenCode](https://opencode.ai).

Point your agent at a folder of raw clips, describe the video you want, and get back a
finished, beat-synced cut — shot selection, auto-matched music, subtitles, transitions,
and picture-in-picture. No timeline, no editing software, no GPU.

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

Restart your agent, then say:
*"Edit the clips in ./footage into a 30-second travel vlog with beat-synced cuts."*

Requires Python 3.11–3.13, `ffmpeg`, and a [Cassette account](https://trycassette.online).
Per-host setup for OpenCode, Hermes, and any other MCP host is in the
[README](https://github.com/Cassette-Editor/oh-my-cassette#-quick-start).

## Documentation

- [Showcase](showcase.md) — six real case videos with the exact prompt, inputs, and processing time for each
- [Development and troubleshooting](development.md) — configuration reference, transports, diagnostics, common runtime problems
- [Changelog](https://github.com/Cassette-Editor/oh-my-cassette/blob/main/CHANGELOG.md) — release history
- [Support and scope](https://github.com/Cassette-Editor/oh-my-cassette/blob/main/SUPPORT.md) — what this plugin covers versus the hosted Cassette service
- [Contributing](https://github.com/Cassette-Editor/oh-my-cassette/blob/main/CONTRIBUTING.md) — development setup and guidelines

## 简体中文

- [中文说明](https://github.com/Cassette-Editor/oh-my-cassette/blob/main/README.zh-cn.md)
- [案例展示](showcase.zh-cn.md)
- [开发与排查](development.zh-cn.md)

## How it works

The plugin runs locally beside your agent as a stdio MCP server — it opens no port — and
handles media ingestion, edit planning, and job supervision. Editing and rendering happen
on [Cassette](https://trycassette.online), a separate hosted service.

Every editing turn returns a timeline delta, a CTL digest, and a contact sheet, so you
review the plan before a frame is rendered. The runtime is host-neutral and sessions live
in a host-agnostic data directory, so an edit started in one host can continue in another.

The plugin is MIT licensed — all of it, including the MCP server and the skill.
[Source on GitHub](https://github.com/Cassette-Editor/oh-my-cassette).
