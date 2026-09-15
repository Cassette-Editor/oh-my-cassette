---
title: Oh My Cassette
---

Oh My Cassette is an open-source MCP server that lets [Claude Code](https://claude.com/claude-code),
[Codex](https://github.com/openai/codex), [OpenCode](https://opencode.ai) and
[Hermes Agent](https://github.com/nousresearch/hermes-agent) edit video through the
[Cassette](https://trycassette.online) editing agent.

Point your agent at a folder of clips, describe the video you want, and the Cassette agent builds the
timeline while you watch it in the browser. Every turn comes back with what changed (`v3→v4`), a
digest of the timeline, and an editor link. Undo, redo and export are one sentence away.

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

```bash
# Hermes, OpenCode, any MCP host
uvx oh-my-cassette==0.5.0
```

Restart your agent, then say:
*"Import the clips in ./footage and cut a 30-second travel vlog with a title at the start."*

Requires [uv](https://docs.astral.sh/uv/), `ffmpeg`, and a reachable Cassette backend
(`CASSETTE_API_URL`; the local development stack by default). Per-host setup is in the
[README](https://github.com/Cassette-Editor/oh-my-cassette#install).

## Documentation

- [Showcase](showcase.md) — six real case videos with the exact prompt, inputs, and processing time for each
- [Development and troubleshooting](development.md) — architecture, configuration reference, tests, common errors
- [Backend changes](https://github.com/Cassette-Editor/oh-my-cassette/blob/main/docs/v2/backend-changes.md) — what the Cassette backend still needs for video import
- [Changelog](https://github.com/Cassette-Editor/oh-my-cassette/blob/main/CHANGELOG.md) — release history
- [Support and scope](https://github.com/Cassette-Editor/oh-my-cassette/blob/main/SUPPORT.md) — what this plugin covers versus the Cassette service
- [Contributing](https://github.com/Cassette-Editor/oh-my-cassette/blob/main/CONTRIBUTING.md) — development setup and guidelines
- [Privacy](privacy.md) — what leaves your machine, what stays on it, and how to remove it
- [Terms of use](terms.md) — the MIT licence in plain language, and where this plugin ends and Cassette begins

## 简体中文

- [中文说明](https://github.com/Cassette-Editor/oh-my-cassette/blob/main/README.zh-cn.md)
- [案例展示](showcase.zh-cn.md)
- [开发与排查](development.zh-cn.md)

## How it works

The server runs locally beside your agent over stdio and opens no port. It uploads media through
presigned URLs, sends each editing request to the Cassette agent runtime as one durable run, follows
the run's event stream, and reads the versioned project document back when the run ends. Editing and
rendering happen on the Cassette backend you point it at.

Nine tools cover the whole loop: project, import, run, answer, status, stop, timeline, history, export.
The plugin is MIT licensed, including the skill.
[Source on GitHub](https://github.com/Cassette-Editor/oh-my-cassette).
