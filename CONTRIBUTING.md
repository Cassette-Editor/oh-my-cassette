# Contributing to Oh My Cassette

Thanks for helping. This repository is a single Python package (`src/oh_my_cassette`) that every host
runs the same way, plus one skill that is copied to two places. The package is a generic bridge to
the Cassette MCP service: it never names a backend tool, and everything the two sides agree on is in
[docs/v3/contract.md](docs/v3/contract.md). Changes must keep that contract intact, or change the
document, the code and `tests/reference_remote.py` together.

## Conventional commits

Release Please derives versions and changelog entries from commits on `main`:

- `feat: …` — user-visible feature;
- `fix: …` — bug fix;
- `feat!: …` or a `BREAKING CHANGE:` footer — breaking change;
- `docs: …`, `ci: …`, `chore: …`, `test: …`, and `refactor: …` — no release bump by themselves.

PRs are squash-merged using the PR title, so the title must itself be a conventional commit line.
Branch from `main` and target `main`. The `release` branch is machine-owned (see [RELEASING.md](RELEASING.md)).

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
- The live test runs against a local Cassette-Editor stack and its MCP service, and is skipped by
  default (it exports, which renders through the backend's configured provider):

  ```bash
  RUN_CASSETTE_LIVE=1 CASSETTE_AUTH_TOKEN=... uv run pytest tests/live -q -rs
  ```

CI installs ffmpeg and runs lint and the suite on Linux only (Python 3.11 and 3.13), builds the wheel
and starts it once with `uvx`. Keep it credential-free and deterministic.

## Changing the contract

1. Change [docs/v3/contract.md](docs/v3/contract.md) and `src/oh_my_cassette/contract.py` together;
   v1 only grows by optional fields.
2. Teach `tests/reference_remote.py` the change and test it through the bridge.
3. Add a conformance check, with a broken variant, when a service could get it wrong.
4. A new `bridge.*` error code goes into the contract's table and both skill copies
   (`skills/cassette-video-edit/SKILL.md` and `.agents/skills/cassette-video-edit/SKILL.md`).
5. Update `README.md` and `README.zh-cn.md` together.

## What not to commit

`.venv`, `cassette-exports/`, real tokens or passwords, media beyond the tiny fixtures in
`tests/fixtures/`, and service URLs that are not the local defaults.
