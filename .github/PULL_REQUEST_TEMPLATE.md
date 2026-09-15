<!--
PR titles are the squash-commit message and Release Please reads them, so the
title itself must be a conventional commit line:
  feat: …   fix: …   docs: …   ci: …   chore: …   test: …   refactor: …
  feat!: … or a BREAKING CHANGE: footer for breaking changes
-->

## What and why

<!-- One or two sentences. Link the issue if there is one. -->

## Verification

<!--
CONTRIBUTING.md asks for real evidence, not just "tests pass". Paste the commands
you ran and what came back.
-->

- **Hosts tested:** <!-- Claude Code / Codex / OpenCode / Hermes -->
- **Backend:** <!-- local stack (AGENT_AUTH_ENABLED=false) / deployed / fake only -->

```
# commands run + relevant output
```

## Checklist

- [ ] PR title is a conventional commit line
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` pass
- [ ] `uv run pytest -q` passes locally
- [ ] CI stays deterministic and credential-free — no real backend calls, no secrets in fixtures or the PR body
- [ ] `contracts/` recaptured if a backend payload changed
- [ ] Both skill copies updated together (`skills/` and `.agents/skills/`) if a tool changed
- [ ] `README.md` and `README.zh-cn.md` kept as matching counterparts, if docs changed
- [ ] No runtime state, media, exports, or `.env` committed

## Scope note

The editing engine — shot selection, pacing, rendering — lives in the Cassette backend, not this
repository. Changes to *how the AI edits* can't be merged here; this repo covers the client, its
tools and host integrations. See [SUPPORT.md](../blob/main/SUPPORT.md).
