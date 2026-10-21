# Releasing Oh My Cassette

Releases are automated with [Release Please](https://github.com/googleapis/release-please) and
published to PyPI with trusted publishing. Nothing is uploaded by hand, and nothing publishes
without a maintainer's approval.

## What a release changes

The standing release PR (`release-type: python`) bumps, in one commit:

- `pyproject.toml` `version` and `src/oh_my_cassette/__init__.py` (`x-release-please-version` marker);
- `.release-please-manifest.json` and `CHANGELOG.md`;
- the bare version fields in `.codex-plugin/plugin.json`, `.claude-plugin/plugin.json`,
  `.claude-plugin/marketplace.json` and `server.json` (`extra-files` in `release-please-config.json`).

Release Please cannot rewrite strings inside other text, so `release-please.yml` then runs a sed on
the release PR branch and pushes a `chore: pin host manifests and docs to X.Y.Z` commit that moves:

- the `oh-my-cassette==X.Y.Z` pins in `.mcp.json`, `.codex-plugin/plugin.json`, `opencode.json`, both
  READMEs, `llms.txt`, `llms-install.md`, `SUPPORT.md`, `SECURITY.md` and `docs/`;
- the `"ref": "vX.Y.Z"` of both plugin marketplaces (`.claude-plugin/marketplace.json`,
  `.agents/plugins/marketplace.json`), and the skill download URLs in both READMEs, to the tag the
  release will create.

A pull request or push made with `GITHUB_TOKEN` starts no workflow, so `release-please.yml` also
dispatches `ci.yml` on the release PR branch. That run produces the `ci-ok` check `main` requires, and
`tests/test_manifests.py` in it fails if any version, pin or tag reference disagrees.

Merging the release PR creates the `vX.Y.Z` tag and the GitHub Release. `release-please.yml` then:

1. checks out the tag and verifies that it pins its own version and that both marketplaces name it;
2. dispatches `publish-pypi.yml` on the tag. The run waits in the `pypi` environment for a
   maintainer's approval (**Actions → the run → Review deployments**); after it, `uv build`, a check
   that the tag equals the package version, publishing the wheel and sdist with OIDC (no API token),
   and once PyPI serves the version, publishing `server.json` to the MCP registry
   (`mcp-publisher login github-oidc`, the binary checked against its release checksum; ownership is
   proven by the `mcp-name: io.github.Cassette-Editor/oh-my-cassette` marker in the README that PyPI
   shows).

## Update channels

| Channel | Runtime comes from | Manifests come from |
| --- | --- | --- |
| Claude Code plugin, Codex plugin | PyPI (`uvx oh-my-cassette==X.Y.Z`, pinned in the manifests) | the `vX.Y.Z` tag the marketplace entries on `main` name |
| OpenCode, Hermes, any MCP host | PyPI, the pin the user wrote in their config | — |
| MCP registry | PyPI | `server.json` on the tag |

Tags cannot be moved or deleted (a ruleset on `refs/tags/v*`), so what a plugin install fetches is
exactly what was reviewed and released.

The `release` branch is frozen at 0.5.0 and takes no commits (a ruleset blocks updates and
deletion). It exists for 0.4 installs: their plugin marketplaces pinned `ref: release`, and their
runtime reads `release/version.txt` (`0.5.0`, never bumped) once a day and offers its update command
when that number is newer. Any version above 0.4 is enough for them to reach the current release.

## PyPI trusted publishing

PyPI lists `Cassette-Editor/oh-my-cassette`, workflow `publish-pypi.yml`, environment `pypi` as the
trusted publisher of `oh-my-cassette`; no token exists. The `pypi` GitHub environment:

- requires a reviewer (a maintainer approves each upload; admins cannot bypass it);
- deploys only from tags matching `v*`.

A `publish-pypi.yml` run on a branch is a rehearsal that uploads to TestPyPI through the `testpypi`
environment.

## Checklist

1. On `main`, run the credential-free set and the wheel smoke test:

   ```bash
   uv run ruff check . && uv run ruff format --check .
   uv run pytest -q -rs
   uv build && uvx --from dist/*.whl oh-my-cassette --version
   ```

2. Run the live test against the Cassette service with an invited account (maintainers):

   ```bash
   RUN_CASSETTE_LIVE=1 uv run pytest tests/live -q -rs
   ```

3. Validate the plugin manifests:

   ```bash
   claude plugin validate --strict .claude-plugin/plugin.json
   claude plugin validate --strict .claude-plugin/marketplace.json
   ```

   A gitignored local `CLAUDE.md` at the repository root makes the first command warn (and fail
   under `--strict`); run it from a clean checkout or move the file aside. Anything else is a
   regression.

4. Review the release PR: version, changelog, the `chore: pin host manifests and docs` commit, and a
   green `ci-ok` from the dispatched CI run.
5. Merge. Approve the `pypi` deployment of `publish-pypi.yml`, then watch it and `release-please.yml`
   finish.
6. Verify:

   ```bash
   uvx oh-my-cassette==X.Y.Z --version
   curl -s "https://registry.modelcontextprotocol.io/v0/servers?search=io.github.Cassette-Editor/oh-my-cassette" | head -c 400
   ```

7. Spot-check the host channels:

   ```bash
   claude plugin marketplace update cassette-editor && claude plugin update oh-my-cassette@cassette-editor
   codex plugin marketplace upgrade cassette-editor && codex plugin add oh-my-cassette@cassette-editor
   ```

8. Glama builds its listing from the build spec saved at
   `https://glama.ai/mcp/servers/Cassette-Editor/oh-my-cassette/admin/dockerfile`, not from the
   `Dockerfile` here. When the entry point or the install changes, update the spec there (build steps
   `uv venv /opt/venv --python 3.13` and `uv pip install --python /opt/venv/bin/python .`, CMD
   `mcp-proxy -- /opt/venv/bin/oh-my-cassette`).

## Repository settings

- Actions may create pull requests (for Release Please) and run only from pinned SHAs.
- Squash merge, PR title as the default commit message.
- `main`: the `ci-ok` check is required, for administrators too.
- Rulesets: `release` and `refs/tags/v*` cannot be updated or deleted.
- `pypi` environment as above. No repository secrets are required for releasing.
- Keep private vulnerability reporting, secret scanning, push protection and Dependabot alerts
  enabled.
