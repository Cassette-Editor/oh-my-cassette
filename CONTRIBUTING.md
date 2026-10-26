# Contributing to Oh My Cassette

Thanks for helping. This repository is a single Python package (`src/oh_my_cassette`) that every host
runs the same way, plus one skill that is copied to two places. The package is a generic bridge to
the Cassette MCP service: it never names a backend tool, and everything the two sides agree on is in
`src/oh_my_cassette/contract.py`. Changes must keep that contract intact, or change `contract.py`,
`tests/reference_remote.py` and the Cassette MCP service together.

## Conventional commits

Release Please derives versions and changelog entries from commits on `main`:

- `feat: …` — user-visible feature;
- `fix: …` — bug fix;
- `feat!: …` or a `BREAKING CHANGE:` footer — breaking change;
- `docs: …`, `ci: …`, `chore: …`, `test: …`, and `refactor: …` — no release bump by themselves
  (`release-please-config.json` hides them from the changelog).

PRs are squash-merged using the PR title, so the title must itself be a conventional commit line.
Branch from `main` and target `main`; a PR merges once the `ci-ok` check is green. The `release`
branch is frozen at 0.5.0 for 0.4 installs and takes no commits (see [RELEASING.md](RELEASING.md)).

By contributing you agree that your contribution is licensed under the MIT License of this
repository.

## Development setup

```bash
uv sync --group dev          # creates .venv with the package, pytest, ruff, starlette, uvicorn
uv run pytest -q -rs         # reference service on loopback, no network
uv run ruff check .
uv run ruff format .
```

`ffmpeg`/`ffprobe` must be on PATH for `tests/test_prepare.py`, which runs the real ffmpeg; without
them that module is skipped.

Run the server from the checkout inside a host while you work:

```bash
claude mcp add cassette-dev -e OH_MY_CASSETTE_LOG=DEBUG -- uv run --directory "$PWD" oh-my-cassette
```

## Tests

- `tests/reference_remote.py` is an executable reference service for the contract (token, `prepare`,
  the upload handshake, downloads, bounded waits). The bridge tests drive the real bridge against it
  through a real MCP client; `tests/test_conformance.py` runs `oh-my-cassette check` against it and
  against broken variants.
- `tests/test_prepare.py` transcodes generated samples with the real ffmpeg and checks every rendition
  with ffprobe; `tests/test_local_jobs.py` covers the `preparing` status and attaching to running work.
- `tests/test_manifests.py` pins the version and the `uvx oh-my-cassette==X.Y.Z` pins across every
  manifest, checks the host timeouts and environment, and checks that both skill copies are identical,
  name no backend tool and cover every `bridge.*` error code.
- The live test is for maintainers: it runs against a Cassette MCP service and an invited account
  (production by default, or a local Cassette stack through `CASSETTE_MCP_URL`), and is skipped by
  default because it imports media and exports, which renders through the service's provider:

  ```bash
  uvx oh-my-cassette login                     # once, or set CASSETTE_AUTH_TOKEN
  RUN_CASSETTE_LIVE=1 uv run pytest tests/live -q -rs
  ```

CI runs lint and the suite on Linux only (Python 3.11, 3.13 and 3.14), builds the wheel and starts it
once with `uvx`; the `ci-ok` job sums them up. It installs no ffmpeg, whose package mirrors stalled its
runs, so `tests/test_prepare.py` runs only locally: run the whole suite with ffmpeg before a release. Keep it credential-free and
deterministic.

## Changing the contract

1. Change `src/oh_my_cassette/contract.py` and the Cassette MCP service together; v1 only grows by
   additions.
2. Teach `tests/reference_remote.py` the change and test it through the bridge.
3. Add a conformance check, with a broken variant, when a service could get it wrong.
4. A new `bridge.*` error code goes into both skill copies' error table
   (`skills/cassette-video-edit/SKILL.md` and `.agents/skills/cassette-video-edit/SKILL.md`).
5. Update `README.md` and `README.zh-cn.md` together.

## What not to commit

`.venv`, `cassette-exports/`, real tokens or passwords, media beyond the tiny fixtures in
`tests/fixtures/`, and service URLs that are not the local defaults.
