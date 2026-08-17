---
title: Terms of Use
---

# Terms of use — Oh My Cassette plugin

_Last reviewed: 2026-08-17. This file is versioned; `git log docs/terms.md` is the change history._

These terms cover the **Oh My Cassette plugin** — the software in
[this repository](https://github.com/Cassette-Editor/oh-my-cassette). They are a plain-language
summary of how the project is licensed and what it expects of you; they do not add restrictions on
top of the licence.

## The software is MIT licensed

All of it — the MCP server, the skill, and the installer scripts — under the
[MIT licence](https://github.com/Cassette-Editor/oh-my-cassette/blob/main/LICENSE). You may use,
copy, modify, and redistribute it, commercially included, provided the copyright notice and
permission notice travel with it. The licence text governs; nothing here overrides it.

**There is no warranty.** The software is provided "as is". Video editing is destructive to
nobody's originals here — the plugin copies inputs rather than editing them in place — but you are
still responsible for keeping your own backups.

## This is a client for a service it does not operate

The plugin sends your media and your instructions to
[Cassette](https://trycassette.online), a separate hosted service, which performs the editing and
rendering. Using the plugin therefore requires a Cassette account.

- Your use of that service is governed by **your agreement with Cassette**, not by this document.
- The MIT licence on this repository grants you rights to **this software only**. It grants no
  rights to the service, its models, or its output beyond what Cassette gives you.
- Account availability, quotas, pricing, and uptime are Cassette's, not this project's. See
  [`SUPPORT.md`](https://github.com/Cassette-Editor/oh-my-cassette/blob/main/SUPPORT.md) for what
  this project can and cannot help with.

## What is expected of you

- **Rights in your media.** Only upload footage, audio, and images you are entitled to upload and
  to have processed by a third-party service.
- **Music licensing is separate from API access.** Supplying your own Jamendo Client ID grants you
  API quota — it is not a music licence. Every matched track carries its own licence URL and
  attribution requirements, and honouring them is your responsibility.
- **Sensitive material.** Editing happens off your machine. If your footage is confidential or
  regulated, satisfy yourself that sending it to a hosted service is appropriate before you do.
  [`SECURITY.md`](https://github.com/Cassette-Editor/oh-my-cassette/blob/main/SECURITY.md)
  describes the trust boundaries.
- **Credentials.** Cassette generates your password and emails it. Keep the credential file
  protected, and rotate after any disclosure.

## No affiliation

This is an independent open-source project. Claude, Claude Code, Codex, OpenCode, Hermes, Jamendo,
and other named products are trademarks of their respective owners; naming them describes
compatibility and implies no endorsement or affiliation.

## Questions and reports

Functionality questions and bugs: [GitHub issues](https://github.com/Cassette-Editor/oh-my-cassette/issues).
Security or privacy problems: [a private advisory](https://github.com/Cassette-Editor/oh-my-cassette/security/advisories/new).
Anything about your Cassette account, billing, or the service itself: Cassette.
