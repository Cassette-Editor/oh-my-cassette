# Support

## What this repository is

Oh My Cassette is the **client**. It runs locally next to your agent and handles
media ingestion, edit planning, job supervision, and the MCP surface your host
talks to.

The **editing engine is a separate hosted service** ([Cassette](https://trycassette.online)).
It is what actually watches your footage, chooses shots, syncs to the beat,
matches music, and renders. That service is not open source and is not in this
repository.

That split decides where a given problem can be solved, so it's worth knowing
before you file anything.

## Where to take what

| Your situation | Where to go |
|---|---|
| Install fails, auth fails, an MCP tool errors, a file won't ingest, a host won't connect | [Open a plugin bug](https://github.com/Cassette-Editor/oh-my-cassette/issues/new?template=bug.yml) |
| The plugin should do something it doesn't — a tool, a host, better ergonomics | [Open a feature request](https://github.com/Cassette-Editor/oh-my-cassette/issues/new?template=feature.yml) |
| Docs are wrong, missing, or confusing | [Open a docs issue](https://github.com/Cassette-Editor/oh-my-cassette/issues/new?template=docs.yml) — or send a PR |
| The edit came back bad — wrong shots, odd pacing, music that doesn't fit | [Discussions](https://github.com/Cassette-Editor/oh-my-cassette/discussions) |
| Cassette account, access, or billing | [Cassette](https://trycassette.online/signup/) |
| A question, an idea, or a cut you want to show off | [Discussions](https://github.com/Cassette-Editor/oh-my-cassette/discussions) or [Discord](https://discord.gg/qd9NY4k8d7) |
| A security vulnerability | [Private advisory](https://github.com/Cassette-Editor/oh-my-cassette/security/advisories/new) — see [SECURITY.md](./SECURITY.md) |

Not sure which side a problem is on? Ask in Discussions. Guessing wrong costs
you nothing and we would rather route it than have you not report it.

## Before you file a bug

Run the diagnostic for your host and include the output. It reports runtime
bootstrap, protected config, transport, and media roots, and it does not print
credentials:

```bash
python3 scripts/diagnose_local_mcp.py   # Claude Code / Codex / OpenCode
python3 scripts/diagnose_install.py     # Hermes
```

Several common errors are self-explaining: `auth_required` returns the exact
private setup command, `source_path_not_allowed` names the trusted-root problem,
and `browser_session_lost` explains why a browser job can't survive a restart.

## Why the backend isn't open

The editing service is a separate product that isn't public yet. This plugin
exists so the workflow is usable — and inspectable — from real agents today,
rather than waiting. Everything on the client side is MIT licensed and open to
contribution; questions about the service's own roadmap or availability are best
asked in Discussions, where we can answer them directly.
