---
title: Privacy
---

# Privacy notice — Oh My Cassette plugin

_Last reviewed: 2026-08-17. This file is versioned; `git log docs/privacy.md` is the change history._

This notice covers the **Oh My Cassette plugin** — the local MCP server, the skill, and the
installer scripts in [this repository](https://github.com/Cassette-Editor/oh-my-cassette).

[Cassette](https://trycassette.online) is a **separate hosted service** that does the editing and
rendering. This project does not operate it, and this notice does not speak for it. Anything you
send there is governed by whatever agreement you have with Cassette.

## The short version

The plugin runs entirely on your machine and opens no port. This project runs no servers, and the
code contains **no telemetry, no analytics, and no crash reporting** — nothing reports back to us.
Data leaves your machine only when a tool you called needs to send it somewhere, and every
destination is listed below.

## What leaves your machine

### Cassette, when you sign in and edit

Editing is a remote operation, so this is the substantial one. Over the course of a session the
plugin sends:

| What | When |
|---|---|
| Your account email and password | `cassette_login` / `setup_local_mcp.py`, exchanged for a token |
| The media files you granted access to, as raw bytes | `cassette_ingest_media`, then the upload on the first job |
| Your editing request, **verbatim** as you typed it | every `cassette_run_job` turn |
| Project and timeline state, and answers to the agent's questions | during a turn |
| A render request, and the download of the finished file | only on an export turn |

The endpoint list lives in the module header of
[`core/api_transport.py`](https://github.com/Cassette-Editor/oh-my-cassette/blob/main/core/api_transport.py)
and is the authoritative description of the wire traffic.

Only files under roots you explicitly granted can be read at all — see
[granting access to media](https://github.com/Cassette-Editor/oh-my-cassette/blob/main/llms-install.md#granting-access-to-media).
Nothing is uploaded on startup, in the background, or on a schedule.

### Music providers, only if you call the music tools

`cassette_match_bgm`, `cassette_match_exact_bgm`, and `jamendo_music_matcher` send **search text**
— track titles, artist names, or a mood description — to the provider you invoked. They never send
your media. Providers are Jamendo (using the read-only Client ID you supply yourself), a
free-to-use music catalogue API, and, for exact-track matching, public third-party mirror endpoints
for the configured sources. If you never call these tools, nothing is sent to any of them.

### GitHub, for the update check

At most once every 24 hours the server fetches
`raw.githubusercontent.com/.../release/version.txt` to tell you when a newer release exists. It is
a plain unauthenticated `GET`; the request carries no identifier beyond what any HTTP request
reveals. Set `CASSETTE_UPDATE_CHECK=0` to turn it off.

## What stays on your machine

- **Credentials.** The verified email and password are stored in a `0600` file inside a `0700`
  config directory. Symlinked, non-regular, wrong-owner, and overly permissive files are rejected.
  Access and refresh tokens are held in memory only and never written to disk.
- **Session state.** Media manifests, job records, and continuation records live under the data
  root. Treat that directory as private runtime state.
- **Previews and exports.** Contact sheets and rendered files are written under
  `<data-root>/cassette/exports/<job_id>/`.

Anyone who can read the credential file can use the account. Use a dedicated account where you can,
and rotate immediately after any accidental disclosure.

## What the plugin will not do

- It exposes **no generic file-reading tool** — there is no way to ask it for an arbitrary path.
- Media is returned as validated metadata and MCP resource links, **never embedded bytes**.
- An artifact is returned only when it resolves inside the export directory for that job.
- Public tool results strip continuation state, prompts, source paths, delivery targets, and raw
  output paths.
- Logs are redacted and go to stderr; stdout carries protocol only. Credentials are never logged.

## Your controls

| You want to | Do this |
|---|---|
| Limit what can be read | Grant narrow roots — never your whole home directory |
| Forget this machine's password | `setup_local_mcp.py --logout` (the Cassette account is untouched) |
| Remove everything local | Delete the config and data directories listed in [`llms-install.md`](https://github.com/Cassette-Editor/oh-my-cassette/blob/main/llms-install.md#uninstalling) |
| Stop the update check | `CASSETTE_UPDATE_CHECK=0` |
| Delete what Cassette holds | Contact Cassette — this project cannot reach their storage |

## Reporting

Report a privacy or security problem privately through
[GitHub security advisories](https://github.com/Cassette-Editor/oh-my-cassette/security/advisories/new),
not a public issue. [`SECURITY.md`](https://github.com/Cassette-Editor/oh-my-cassette/blob/main/SECURITY.md)
documents the trust boundaries in more detail.
