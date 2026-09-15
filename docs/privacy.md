---
title: Privacy
---

# Privacy notice — Oh My Cassette

_Last reviewed: 2026-09-15. This file is versioned; `git log docs/privacy.md` is the change history._

This notice covers the **Oh My Cassette MCP server and skill** published from
[this repository](https://github.com/Cassette-Editor/oh-my-cassette).

[Cassette](https://trycassette.online) is a **separate service** that does the editing and rendering.
This project does not operate it, and this notice does not speak for it. Anything you send there is
governed by whatever agreement you have with the operator of the backend you configured.

## The short version

The server runs on your machine, over stdio, and opens no port. The code contains **no telemetry, no
analytics, no crash reporting and no update check** — nothing reports back to us. Data leaves your
machine only towards the one backend you configured with `CASSETTE_API_URL` (and the storage it
issues presigned URLs for), and only when a tool you called needs to send it.

## What leaves your machine

| What | When |
|---|---|
| A bearer token, or your email and password | Only if you configured `CASSETTE_AUTH_TOKEN` or `CASSETTE_EMAIL`/`CASSETTE_PASSWORD`. Email and password go once to `/api/agent-auth/verify`; the returned token is cached locally. Nothing is sent when neither is set. |
| The media files you named, as bytes | `cassette_import`. Video is first transcoded locally with ffmpeg; the original plus the transcoded canonical and preview files are uploaded. Audio and images are uploaded as they are. File names and SHA-256 hashes are sent with them. |
| Your editing request, **verbatim** as you typed it | every `cassette_run` turn, and your answers in `cassette_answer` |
| The project document and its history | read back after each turn; history commands (`undo`, `redo`, `restore_before`) are sent to the backend |
| The timeline as an export manifest, and the download of the rendered file | only when you call `cassette_export` |

The plugin sends the working directory's project binding to nobody; project ids are generated
locally for anonymous projects.

## What stays on your machine

Under `~/.oh-my-cassette` (override with `OH_MY_CASSETTE_HOME`):

| File | Contents |
|---|---|
| `state.json` (mode 0600) | project bindings per working directory, project and chat session ids, the media file ids the backend assigned and the local paths they came from, SHA-256 hashes for duplicate detection, event cursors |
| `credentials.json` (mode 0600) | the verified token when you use email/password; absent otherwise |
| `artifacts/<project>/` | contact sheets rendered locally from your own files |

Exports are written to `./exports` in the working directory (or the `output_dir` you gave). Temporary
transcodes live in a temporary directory that is deleted when the import finishes.

## What the plugin will not do

- Read files you did not name in a tool call. It reads exactly the paths passed to `cassette_import`.
- Send media anywhere but the presigned upload URL the configured backend issued.
- Contact any third party: no music providers, no GitHub update check, no analytics.
- Keep your password: only the verified token is cached, and only when you chose email/password.

## Your controls

| You want to | Do this |
|---|---|
| Forget a project's local binding | `cassette_project` with `action=forget` (the backend project is untouched) |
| Forget this machine's cached token | delete `~/.oh-my-cassette/credentials.json` |
| Remove everything local | delete `~/.oh-my-cassette` and `./exports`; `uv cache clean oh-my-cassette` removes the package |
| Point at a different backend | change `CASSETTE_API_URL` in the host's server environment |
| Delete what the backend holds | contact the backend's operator — this project cannot reach their storage |

## Reporting

Privacy questions or concerns about the plugin: open a
[docs issue](https://github.com/Cassette-Editor/oh-my-cassette/issues/new?template=docs.yml). A
vulnerability: use a [private advisory](https://github.com/Cassette-Editor/oh-my-cassette/security/advisories/new).
