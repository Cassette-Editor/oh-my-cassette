---
title: Privacy
---

# Privacy notice — Oh My Cassette plugin

_Last reviewed: 2026-10-21, for 0.5.0. This file is versioned; `git log docs/privacy.md` is the change history._

This notice covers the **Oh My Cassette plugin** — the local MCP bridge and the skill in
[this repository](https://github.com/Cassette-Editor/oh-my-cassette).

[Cassette](https://trycassette.online) is a **separate hosted service** that does the editing and
rendering. This notice does not speak for it. Anything you send there is governed by your agreement
with Cassette.

## The short version

The bridge runs on your machine as a stdio child of your agent and opens no port (`login` briefly
binds a loopback callback for the browser sign-in). It contains **no telemetry, no analytics, no
crash reporting and no update check** — nothing reports back to this project. It talks to the
Cassette MCP endpoint (`https://mcp.trycassette.online/mcp` unless you set `CASSETTE_MCP_URL`), to
the sign-in service that endpoint names, and to the upload and download URLs the endpoint hands it
(the endpoint's own origin, or public HTTPS hosts only).

## What leaves your machine

| What | To | When |
|---|---|---|
| An OAuth access token, as a bearer token | the service | every call to the service, and to upload or download URLs on the service's own origin; presigned URLs on other origins get no credential |
| The refresh token, and the grant to revoke | the sign-in service | when an expiring access token is renewed, and on `logout` |
| `oh-my-cassette/<version>` as the User-Agent | the service | every request |
| Your editing requests and answers, as your agent passes them | the service | every tool call your agent makes |
| The workspace: an opaque identifier for its directory and the directory's name (not its path) | the service | every tool call |
| The host's name and version (e.g. Claude Code 2.x), as the host reports it or `CASSETTE_MCP_HOST` sets it | the service | every tool call |
| For each file a call names: its name, its path relative to the workspace, its size and SHA-256, and its type | the service | when a tool call names it |
| The files themselves, after local preparation | the upload URLs the service hands out | only when a tool call names them; nothing uploads on startup, in the background or on a schedule |

Before upload, video is transcoded on your machine with ffmpeg into the renditions the service
declares, which drops the source's metadata (camera, location, timestamps). Audio and images the
service takes as they are upload **unchanged, with their embedded metadata** — for example a photo's
EXIF GPS position or a song's ID3 tags; ones it does not take as they are get converted. Files come
only from the workspace or `CASSETTE_ALLOWED_ROOTS`, and only media types unless you set
`CASSETTE_UPLOAD_ANY_TYPE`.

Sign-in happens in your browser on Cassette's own pages (an emailed code and a consent screen); the
bridge never sees a password.

## What stays on your machine

- **Tokens.** OAuth access and refresh tokens live only in the operating-system keyring (macOS
  Keychain, Windows Credential Vault or Linux Secret Service).
- **Connection metadata.** The endpoint and account you signed in to, in `~/.config/oh-my-cassette`.
  It contains no token.
- **Prepared media.** Transcoded files sit in a private directory under `CASSETTE_TEMP_DIR`
  (default: the system temporary directory) only until they are uploaded.
- **Exports.** Downloaded videos are saved in `cassette-exports/` in your workspace (or
  `CASSETTE_DOWNLOAD_DIR`) and stay there.

## What Cassette keeps

A project the bridge starts is a cloud project. Cassette deletes it, with its media, after three idle
days; every use pushes that back, and each answer says when it is due (`retention_expires_at`). A
project made in Cassette for Mac stays on that Mac and is kept. For anything else Cassette stores,
ask Cassette.

## Your controls

| You want to | Do this |
|---|---|
| Limit what can be read | Keep media in the workspace, or grant narrow `CASSETTE_ALLOWED_ROOTS` — never your whole home directory |
| Sign this machine out | `uvx oh-my-cassette==0.5.1 logout` — revokes the grant, then deletes the keyring entry |
| Remove everything local | Sign out, delete `~/.config/oh-my-cassette`, and uninstall the plugin |
| Delete a cloud project now | Delete it in Cassette, or leave it idle for three days |

## Reporting

Report a privacy or security problem privately through
[GitHub private vulnerability reporting](https://github.com/Cassette-Editor/oh-my-cassette/security/advisories/new),
not a public issue. [`SECURITY.md`](https://github.com/Cassette-Editor/oh-my-cassette/blob/main/SECURITY.md)
documents the trust boundaries in more detail.
