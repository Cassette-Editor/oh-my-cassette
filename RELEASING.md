# Releasing Oh My Cassette

Releases are automated with [Release Please](https://github.com/googleapis/release-please). A standing release PR updates `version.txt`, `.release-please-manifest.json`, `CHANGELOG.md`, and the configured extra version fields for:

- Hermes `plugin.yaml`;
- Codex `.codex-plugin/plugin.json`;
- Claude `.claude-plugin/plugin.json`;
- `.claude-plugin/marketplace.json`; and
- `mcp_plugin/__init__.py`.

Merging the release PR creates `vX.Y.Z` and the GitHub Release.

## Update channels

| Channel | Tracks | Why |
| --- | --- | --- |
| Codex, Claude Code | `release` branch | Their marketplace entries pin `ref: release`; the release-please workflow force-pushes that branch to each new tag, so these hosts install exactly what was tested. |
| Hermes | `main` | `hermes plugins install` clones the default branch and `update` pulls it. Hermes has no ref pinning, so this channel is rolling by nature. |
| OpenCode | whatever the user cloned | `scripts/install_opencode.py` registers a checkout; the README clones `--branch release`. |

Both marketplace *catalogs* are still read from `main`, so listings stay current while plugin code comes from the tag. Keep `main` green — Hermes users get it immediately.

Do not enable branch protection on `release`; the workflow force-pushes it. Never commit to `release` directly — the next release overwrites it.

Releases carry no uploaded assets on purpose. No host consumes an archive (Claude's plugin sources are git or npm, Codex and Hermes clone, OpenCode takes a command), and GitHub already serves auto-generated source zip/tar per tag.

## Release checklist

1. Review the release PR version and changelog. Confirm every manifest and marketplace reports the same semantic version.
2. Run the complete credential-free acceptance set:

   ```bash
   .venv/bin/python -m compileall -q .
   .venv/bin/python -m pytest -q -rs -n 4 --dist loadfile
   ```

3. Run the Codex plugin validator and the official Claude validators:

   ```bash
   .venv/bin/python /path/to/plugin-creator/scripts/validate_plugin.py .
   claude plugin validate --strict .claude-plugin/plugin.json
   claude plugin validate --strict .claude-plugin/marketplace.json
   ```

4. Smoke-install from a clean config with the currently supported Codex and Claude CLI versions. Confirm Codex lists `oh-my-cassette@cassette-editor`; confirm Claude reports exactly one host-neutral skill and one `cassette` MCP server.
5. Trigger the maintainer E2E workflow. It runs a real edit through the local MCP entrypoint for both host labels:

   ```bash
   gh workflow run e2e.yml
   gh run watch
   ```

   For local acceptance, use only ephemeral environment variables:

   ```bash
   CASSETTE_AUTH_EMAIL=… CASSETTE_AUTH_PASSWORD=… \
   .venv/bin/python scripts/e2e_local_mcp.py \
     --host codex \
     --media /absolute/path/to/a-real-test-clip.mp4 \
     --instruction "Make a short captioned video."
   ```

6. Record the live matrix in the release/PR notes:

   - API auth and one real MCP edit;
   - Codex guided flow;
   - Claude guided flow;
   - completion review and validated artifact link;
   - resume after host restart.

7. Inspect the complete diff and staged files for credentials, private IDs, media, job state, and absolute acceptance paths. Rotate the live test password if it was ever shared outside the repository secret store.
8. Ensure the release PR's required checks run. Release PRs created with `GITHUB_TOKEN` may need to be closed/reopened or have a new commit pushed before workflows trigger.
9. Merge and verify the tag, GitHub Release, marketplace manifests, and `plugin.yaml` on `main`. Confirm the workflow advanced the release channel — these must print the same SHA:

    ```bash
    git fetch origin --tags
    git rev-parse "vX.Y.Z^{}" origin/release
    ```

10. Spot-check update channels:

    ```bash
    codex plugin marketplace upgrade cassette-editor
    codex plugin add oh-my-cassette@cassette-editor

    claude plugin marketplace update cassette-editor
    claude plugin update oh-my-cassette@cassette-editor

    hermes plugins update cassette
    hermes plugins list

    python3 ~/.oh-my-cassette/scripts/install_opencode.py --sync
    ```

The repository marketplaces are the supported Codex/Claude distribution channel. Do not submit the release to an external public plugin directory as part of this process.

If Release Please proposes the wrong version, use a `Release-As: X.Y.Z` footer on an empty commit to `main`.

## Repository settings

- Enable Actions permission to create and approve pull requests.
- Enable squash merge and use the PR title as the default commit message.
- Protect `main` with the stable `ci-ok` aggregate check and disallow force pushes.
- Keep secret scanning, push protection, Dependabot alerts, and CodeQL enabled.
- Store `CASSETTE_AUTH_EMAIL` and `CASSETTE_AUTH_PASSWORD` as repository secrets for the manual E2E workflow. Use a dedicated, rotatable test account.
- Optionally set `CASSETTE_URL` and `CASSETTE_API_URL` repository variables for non-default deployments.
