---
title: Privacy
---

# Privacy notice — Oh My Cassette

_Last reviewed: 2026-10-02. This file is versioned; `git log docs/privacy.md` is the change history._

This notice covers the **Oh My Cassette MCP server and skill** published from
[this repository](https://github.com/Cassette-Editor/oh-my-cassette).

[Cassette](https://trycassette.online) is a **separate service** that does the editing and rendering.
This project does not operate it, and this notice does not speak for it. Anything you send there is
governed by whatever agreement you have with the operator of the service you configured.

## The short version

The server runs on your machine, over stdio, and opens no port. The code contains **no telemetry, no
analytics, no crash reporting and no update check** — nothing reports back to us. Data leaves your
machine only towards the one Cassette MCP service you configured with `CASSETTE_MCP_URL` (and the
storage it issues presigned URLs for), and only when a tool you called needs to send it.

## What leaves your machine

| What | When |
|---|---|
| Your access token | with every request to the MCP service, and to upload or download URLs only when they have the service's origin |
| The media files you named, as bytes | when you call a tool that takes local files. Video is first transcoded locally with ffmpeg and only the transcoded renditions are uploaded, never the original. Audio and images are uploaded as they are, or converted locally first when the service does not take their format. File names, the path relative to the workspace, sizes, SHA-256 hashes and an ffprobe description of each video go with them. |
| An anonymous workspace id | with every call: the first 32 hex digits of the SHA-256 of the workspace path, and the workspace directory's name |
| The host's name and version | with every call, as the host reported them |
| Everything else you pass to the service's tools | your editing requests, answers, project ids; the service decides what its tools take |

## What stays on your machine

The bridge keeps no state file and no credential cache. Prepared files are written to a temporary
directory (`CASSETTE_TEMP_DIR`, default `<system temp>/oh-my-cassette`) and deleted once they are
uploaded or the preparation fails. Exports are written to `cassette-exports/` in the workspace (or
`CASSETTE_DOWNLOAD_DIR`).

## What the plugin will not do

- Read files you did not name in a tool call, or files outside the workspace and the directories you
  allowed with `CASSETTE_ALLOWED_ROOTS`.
- Send media anywhere but the upload URLs the configured service issued.
- Contact any third party: no music providers, no GitHub update check, no analytics.

## Your controls

| You want to | Do this |
|---|---|
| Remove everything local | delete `cassette-exports/`; `uv cache clean oh-my-cassette` removes the package |
| Point at a different service | change `CASSETTE_MCP_URL` in the host's server environment |
| Stop sending your token | remove `CASSETTE_AUTH_TOKEN`; the service then refuses every call |
| Delete what the service holds | contact the service's operator — this project cannot reach their storage |

## Reporting

Privacy questions or concerns about the plugin: open a
[docs issue](https://github.com/Cassette-Editor/oh-my-cassette/issues/new?template=docs.yml). A
vulnerability: use a [private advisory](https://github.com/Cassette-Editor/oh-my-cassette/security/advisories/new).
