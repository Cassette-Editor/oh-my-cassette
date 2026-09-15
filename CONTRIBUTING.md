# Contributing to Oh My Cassette

Thanks for helping. This repository is a single Python package (`src/oh_my_cassette`) that every host
runs the same way, plus one skill that is copied to two places. Changes must keep the tool contract
(the typed `status` results) and the backend contract (`contracts/`) intact.

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
uv run pytest -q             # fake backend, no network, ~10 s
uv run ruff check src tests
uv run ruff format src tests
```

`ffmpeg`/`ffprobe` must be on PATH: the video-import and contact-sheet tests use them.

Run the server from the checkout inside a host while you work:

```bash
claude mcp add cassette-dev -e OH_MY_CASSETTE_LOG=DEBUG -- uv run --directory "$PWD" oh-my-cassette
```

## Tests

- `tests/fake_cassette/` is a Starlette fake of the backend routes the plugin uses. Every tool has an
  end-to-end test through the real MCP client (`tests/test_tools_smoke.py` and the per-area files).
- `tests/test_models.py` validates the pydantic mirrors against the JSON captured in `contracts/`.
  When the backend changes a payload, recapture the file (see `contracts/README.md`) rather than
  loosening the model.
- `tests/test_manifests.py` pins the version and the `uvx oh-my-cassette==X.Y.Z` pins across every
  manifest, and checks that both skill copies are identical and mention every tool.
- Live tests run against a local Cassette-Editor stack and are skipped by default:

  ```bash
  RUN_CASSETTE_LIVE=1 uv run pytest tests/live -q -rs
  RUN_CASSETTE_LIVE=1 RUN_CASSETTE_LIVE_EXPORT=1 uv run pytest tests/live -q -rs   # also renders
  ```

CI runs lint and the fake-backend suite on Linux only (Python 3.11 and 3.13), builds the wheel and
starts it once with `uvx`. Keep it credential-free and deterministic.

## Adding or changing a tool

1. Implement it in `src/oh_my_cassette/tools/` and register it in `server.py` (add the name to
   `TOOL_NAMES`).
2. Return a dict with a typed `status`; wrap failures with `@guarded` so errors come back as
   `{"status": "failed", "error": {...}}` instead of exceptions.
3. Add the fake route(s) and a test.
4. Document it in `skills/cassette-video-edit/SKILL.md` and copy the file to
   `.agents/skills/cassette-video-edit/SKILL.md` (the manifest test fails if the copies differ or a
   tool is undocumented).
5. Update `README.md` and `README.zh-cn.md` together.

## What not to commit

`.venv`, `exports/`, anything under `~/.oh-my-cassette`, real tokens or passwords, media beyond the
tiny fixtures in `tests/fixtures/`, and backend URLs that are not the local defaults.
