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

- **Hosts tested:** <!-- Claude Code / Codex / OpenCode / Hermes / web demo -->
- **Transport:** <!-- api / browser / both / n/a -->

```
# commands run + relevant output
```

## Checklist

- [ ] PR title is a conventional commit line
- [ ] `uvx ruff@0.15.22 check .` and `uvx ruff@0.15.22 format --check .` pass
- [ ] `pytest` passes locally
- [ ] CI stays deterministic and credential-free — no real Cassette calls, no secrets in fixtures or the PR body
- [ ] Both manifests / marketplaces / `plugin.yaml` updated, if packaging or tool registration changed
- [ ] `README.md` and `README.zh-cn.md` kept as matching counterparts, if docs changed
- [ ] No runtime state, media, exports, or `.env` committed

## Scope note

The editing engine — shot selection, pacing, music, rendering — lives in the
Cassette service, not this repository. Changes to *how the AI edits* can't be
merged here; this repo covers the client, its tools, transports, and host
integrations. See [SUPPORT.md](../blob/main/SUPPORT.md).

See [CONTRIBUTING.md](../blob/main/CONTRIBUTING.md) for architecture rules,
lockfile regeneration, and the live-acceptance procedure.
