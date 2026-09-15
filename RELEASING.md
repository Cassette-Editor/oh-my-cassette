# Releasing Oh My Cassette

Releases are automated with [Release Please](https://github.com/googleapis/release-please) and
published to PyPI with trusted publishing. Nothing is uploaded by hand.

## What a release changes

The standing release PR (`release-type: python`) bumps, in one commit:

- `pyproject.toml` `version` and `src/oh_my_cassette/__init__.py` (`x-release-please-version` marker);
- `.release-please-manifest.json` and `CHANGELOG.md`;
- the bare version fields in `.codex-plugin/plugin.json`, `.claude-plugin/plugin.json`,
  `.claude-plugin/marketplace.json` and `server.json` (`extra-files` in `release-please-config.json`);
- the `uvx oh-my-cassette==X.Y.Z` pins in `.mcp.json`, `.codex-plugin/plugin.json` and
  `opencode.json` — release-please cannot rewrite those strings, so `release-please.yml` runs a sed
  on the release PR branch and pushes a `chore: pin host manifests to X.Y.Z` commit.

`tests/test_manifests.py` fails if any of these disagree, so CI on the release PR is the check.

Merging the release PR creates the `vX.Y.Z` tag and the GitHub Release, which triggers:

1. `publish-pypi.yml` — `uv build`, checks that the tag equals the package version, publishes the
   wheel and sdist through the `pypi` environment with OIDC (no API token), then waits until PyPI
   serves the version and publishes `server.json` to the MCP registry (`mcp-publisher login
   github-oidc`; ownership is proven by the `mcp-name: io.github.Cassette-Editor/oh-my-cassette`
   marker in the README that PyPI shows).
2. `release-please.yml` — force-pushes the `release` branch to the new tag and verifies it.

## Update channels

| Channel | Runtime comes from | Manifests come from |
| --- | --- | --- |
| Claude Code plugin, Codex plugin | PyPI (`uvx oh-my-cassette==X.Y.Z`, pinned in the manifests) | `release` branch (marketplace entries pin `ref: release`) |
| OpenCode, Hermes, any MCP host | PyPI, the pin the user wrote in their config | — |
| MCP registry | PyPI | `server.json` on the tag |

Do not enable branch protection on `release`; the workflow force-pushes it. Never commit to
`release` directly.

## One-time setup: PyPI trusted publishing

The package name `oh-my-cassette` is not registered on PyPI yet. The first release creates it
through a *pending* trusted publisher, so no token ever exists:

1. Create or sign in to the PyPI account that will own the project, with 2FA enabled.
2. Go to **Your account → Publishing → Add a new pending publisher** and fill in:
   - PyPI project name: `oh-my-cassette`
   - Owner: `Cassette-Editor`
   - Repository name: `oh-my-cassette`
   - Workflow name: `publish-pypi.yml`
   - Environment name: `pypi`
3. In the GitHub repository, **Settings → Environments → New environment** named `pypi`. Add
   required reviewers if a human should approve each upload; otherwise leave it open.
4. That is all. The first successful run of `publish-pypi.yml` claims the name and creates the
   project; the pending publisher becomes a normal trusted publisher. If the name is taken by then,
   PyPI refuses the registration and the workflow fails with a clear error — pick another name in
   `pyproject.toml`, the manifests and the docs before releasing.

The name is available as of 2026-09-15 (`https://pypi.org/project/oh-my-cassette/` returns 404).
PyPI normalises names, so `oh_my_cassette` and `oh-my-cassette` are the same project.

## Releasing 0.5.0 (the first PyPI release)

Release Please would compute the next version from the conventional commits since 0.4.19. To force
the number, land the rewrite with a `Release-As: 0.5.0` footer on the merge commit (or on an empty
commit to `main`):

```bash
git commit --allow-empty -m "chore: release 0.5.0" -m "Release-As: 0.5.0"
```

Then follow the checklist below.

## Checklist

1. On `main`, run the credential-free set and the wheel smoke test:

   ```bash
   uv run ruff check src tests && uv run ruff format --check src tests
   uv run pytest -q -rs
   uv build && uvx --from dist/*.whl oh-my-cassette --version
   ```

2. Run the live tests against a local Cassette-Editor stack
   (`AGENT_AUTH_ENABLED=false bun run dev:lambda`):

   ```bash
   RUN_CASSETTE_LIVE=1 RUN_CASSETTE_LIVE_EXPORT=1 uv run pytest tests/live -q -rs
   ```

3. Validate the plugin manifests:

   ```bash
   claude plugin validate --strict .claude-plugin/plugin.json
   claude plugin validate --strict .claude-plugin/marketplace.json
   ```

   A gitignored local `CLAUDE.md` at the repository root makes the first command warn (and fail
   under `--strict`); run it from a clean checkout or move the file aside. Anything else is a
   regression.

4. Review the release PR: version, changelog, and that the `chore: pin host manifests` commit is
   present. Release PRs created with `GITHUB_TOKEN` may need a close/reopen for CI to run.
5. Merge. Watch `publish-pypi.yml` and `release-please.yml` finish.
6. Verify:

   ```bash
   uvx oh-my-cassette==X.Y.Z --version
   git fetch origin --tags && git rev-parse "vX.Y.Z^{}" origin/release   # same SHA
   ```

7. Spot-check the host channels:

   ```bash
   claude plugin marketplace update cassette-editor && claude plugin update oh-my-cassette@cassette-editor
   codex plugin marketplace upgrade cassette-editor && codex plugin add oh-my-cassette@cassette-editor
   ```

## Repository settings

- Actions may create and approve pull requests (for Release Please).
- Squash merge, PR title as the default commit message.
- Protect `main`; do not protect `release`.
- `pypi` environment exists (see above). No repository secrets are required for releasing.
- Keep secret scanning, push protection and Dependabot alerts enabled.
