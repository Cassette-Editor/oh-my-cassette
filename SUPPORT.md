# Support

## What this repository is

Oh My Cassette is the **client**: a local MCP server (PyPI `oh-my-cassette`) plus a skill. It
imports your media, relays your editing requests to the Cassette agent one turn at a time, reads the
timeline back, and downloads exports.

The **editing engine is the Cassette backend** ([Cassette](https://trycassette.online)). It runs the
agent that watches your footage, edits the timeline and renders. It is a separate service and is not
in this repository.

That split decides where a given problem can be solved, so it is worth knowing before you file.

## Where to take what

| Your situation | Where to go |
|---|---|
| Install fails, a host won't connect, an MCP tool errors, a file won't import, an export won't download | [Open a plugin bug](https://github.com/Cassette-Editor/oh-my-cassette/issues/new?template=bug.yml) |
| The plugin should do something it doesn't — a tool, a host, better ergonomics | [Open a feature request](https://github.com/Cassette-Editor/oh-my-cassette/issues/new?template=feature.yml) |
| Docs are wrong, missing, or confusing | [Open a docs issue](https://github.com/Cassette-Editor/oh-my-cassette/issues/new?template=docs.yml) — or send a PR |
| The edit came back bad — wrong shots, odd pacing, the agent misread the request | [Discussions](https://github.com/Cassette-Editor/oh-my-cassette/discussions) |
| Something the backend must change for the plugin (see [docs/v2/backend-changes.md](./docs/v2/backend-changes.md)) | Cassette, with a link to that document |
| Cassette account, access, or billing | [Cassette](https://trycassette.online) |
| A question, an idea, or a cut you want to show off | [Discussions](https://github.com/Cassette-Editor/oh-my-cassette/discussions) or [Discord](https://discord.gg/qd9NY4k8d7) |
| A security vulnerability | [Private advisory](https://github.com/Cassette-Editor/oh-my-cassette/security/advisories/new) — see [SECURITY.md](./SECURITY.md) |

Not sure which side a problem is on? Ask in Discussions. Guessing wrong costs you nothing and we
would rather route it than have you not report it.

## Before you file a bug

Include these, none of which contain credentials:

```bash
uvx oh-my-cassette==0.5.0 --version
```

- the host (Claude Code, Codex, OpenCode, Hermes) and its version;
- the tool call and the full JSON result it returned (every tool returns a typed `status` and, on
  failure, an `error.code`);
- if the server itself misbehaves, its stderr with `OH_MY_CASSETTE_LOG=DEBUG` set in the host's
  server environment.

Several results are self-explaining: `no_project` means no project is bound to the working directory
(call `cassette_project`), `video_import_requires_backend_update` means the backend has not landed
the change in [docs/v2/backend-changes.md](./docs/v2/backend-changes.md), and `project_timeline_locked`
means a run is still active (`cassette_status`, then retry).

## Why the backend is separate

The editing service is a separate product. This plugin exists so the workflow is usable — and
inspectable — from real agents today. Everything on the client side is MIT licensed and open to
contribution; questions about the service's own roadmap or availability are best asked in
Discussions, where we can answer them directly.
