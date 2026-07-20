# Contributing to Oh My Cassette

Thanks for helping. This repository ships four adapters—Codex, Claude, Hermes, and the web demo—so changes must preserve their isolation as well as shared-core behavior.

## Conventional commits

Release Please derives versions and changelog entries from commits on `main`:

- `feat: …` — user-visible feature;
- `fix: …` — bug fix;
- `feat!: …` or a `BREAKING CHANGE:` footer — breaking change;
- `docs: …`, `ci: …`, `chore: …`, `test: …`, and `refactor: …` — no release bump by themselves.

PRs are squash-merged using the PR title, so the title must itself be a conventional commit line.

## Development setup

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python \
  -r requirements-web.txt -r requirements-browser.lock pytest pytest-xdist
.venv/bin/python -m playwright install chromium
```

Run the CI-equivalent checks:

```bash
uvx ruff check .
uvx ruff format --check .
.venv/bin/python -m compileall -q .
.venv/bin/python -m pytest -q -rs -n 4 --dist loadfile
./web_demo/build_frontend.sh
```

`ruff format .` fixes formatting in place; configuration lives in `pyproject.toml`.

Validate native packaging with supported host CLIs:

```bash
python3 /path/to/plugin-creator/scripts/validate_plugin.py .
claude plugin validate --strict .claude-plugin/plugin.json
claude plugin validate --strict .claude-plugin/marketplace.json
```

The CI `native-packaging` job also smoke-installs the plugin with pinned Codex and Claude CLI versions and launches the MCP through `claude mcp list`, which performs a real stdio initialize against every configured server. `mcp-macos-smoke` exercises the locked launcher and real stdio protocol on macOS; the Python matrix covers 3.11 and 3.13 on Ubuntu.

To exercise the MCP inside real hosts locally:

```bash
# Claude project scope: the repo-root .mcp.json launches this checkout directly.
claude mcp list           # run inside the repo; expect "cassette: … ✔ Connected"

# Claude plugin: install from this checkout into an isolated HOME, then health-check.
tmp_home="$(mktemp -d)"
HOME="$tmp_home" claude plugin marketplace add "$PWD" --scope user
HOME="$tmp_home" claude plugin install oh-my-cassette@cassette-editor --scope user
(cd "$(mktemp -d)" && HOME="$tmp_home" claude mcp list)

```

For the Codex side, follow the `Smoke-install with Codex CLI` step in `.github/workflows/ci.yml` — it rewrites the marketplace source to `local`, installs into an isolated `CODEX_HOME`, and asserts the resolved server config with `codex mcp get cassette --json`.

`tests/test_mcp_host_launch.py` covers the same three launch paths deterministically: it parses each host config file, resolves variables the way that host does, and completes initialize + tools/list over stdio.

## Architecture rules

- `mcp_plugin` may reuse repository core modules, but it must not import `web_demo`.
- The web demo keeps its FastAPI server and process-environment config.
- Hermes keeps its hooks, commands, notifier, gateway roots, and `.env` behavior.
- Codex and Claude use the protected host-neutral config/data roots and the host-neutral skill under `skills/`.
- Keep the 11 tool names in parity unless a deliberate compatibility change is approved.
- Never make prompt wording the enforcement boundary for routing, transitions, tool choice, progress, completion, recovery, or export. Use Pydantic schemas, typed persisted state, runtime transition validation, bound tools, and deterministic tests.
- Semantic keyword/regex classifiers and fixed semantic counters must not be added for agent routing or public gate decisions. Mechanical parsing, sanitization, redaction, exact schema checks, and named runtime budgets are allowed.
- MCP stdout is protocol-only. Send diagnostics to stderr.
- Return media as validated metadata/resource links; never embed large media bytes or add a generic file-reading surface.

## Dependencies and generated locks

`requirements-mcp.in` pins the MCP SDK. Regenerate universal locks with the supported `uv` version when dependencies change:

```bash
uv pip compile --python-version 3.11 --universal --no-emit-index-url \
  requirements-mcp.in -o requirements-mcp.lock
uv pip compile --python-version 3.11 --universal --no-emit-index-url \
  requirements-browser.in -o requirements-browser.lock
```

The launcher hashes the lock and reconciles its plugin-managed environment on first start or upgrade. Browser dependencies and Chromium remain optional.

## Tests and live acceptance

PR CI must remain deterministic and credential-free. Mock protocol tests should initialize a real stdio process and cover success, validation/error envelopes, auth, state transitions, restart/resume, cancellation, long-polling, artifact links, path containment, and redaction.

Real Cassette acceptance is maintainer-triggered:

```bash
CASSETTE_AUTH_EMAIL=… CASSETTE_AUTH_PASSWORD=… \
.venv/bin/python scripts/e2e_local_mcp.py \
  --host codex --transport api \
  --media /absolute/path/to/test.mp4 \
  --instruction "Make a short captioned video."
```

Use a realistic non-sensitive clip and ephemeral environment variables. Never paste credentials into test source, command history intended for sharing, logs, a PR body, or fixtures. Rotate any password that was shared in conversation after acceptance.

## Pull requests

- Keep `README.md` and `README.zh-cn.md` natural, semantically matched counterparts.
- Update both plugin manifests and both marketplaces when packaging changes.
- Update `plugin.yaml` when Hermes tool/hook registration changes.
- Adapt `.env.example`, security, release, CI, and troubleshooting docs when configuration changes.
- Preserve unrelated working-tree changes and never commit runtime state.
- Include verification evidence, the tested Codex/Claude CLI versions, and the intentional browser-restart limitation in the PR body.

See [SECURITY.md](./SECURITY.md) before handling credentials or media.
