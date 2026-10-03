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
- **Service:** <!-- local stack + MCP service / deployed / reference service only -->

```
# commands run + relevant output
```

## Checklist

- [ ] PR title is a conventional commit line
- [ ] `uv run ruff check .` and `uv run ruff format --check .` pass
- [ ] `uv run pytest -q -rs` passes locally (with ffmpeg installed)
- [ ] CI stays deterministic and credential-free — no real backend calls, no secrets in fixtures or the PR body
- [ ] `docs/v3/contract.md`, `contract.py` and `tests/reference_remote.py` changed together if the contract changed
- [ ] Both skill copies updated together (`skills/` and `.agents/skills/`) if the skill changed
- [ ] `README.md` and `README.zh-cn.md` kept as matching counterparts, if docs changed
- [ ] No runtime state, media, exports, or `.env` committed

## Scope note

The editing engine — shot selection, pacing, rendering — lives in the Cassette backend, not this
repository. Changes to *how the AI edits* can't be merged here; this repo covers the client, its
tools and host integrations. See [SUPPORT.md](../blob/main/SUPPORT.md).
