# Releasing Oh My Cassette

Releases are automated with [release-please](https://github.com/googleapis/release-please):
every push to `main` updates a standing release PR
(`chore(main): release X.Y.Z`) that bumps `version.txt`,
`.release-please-manifest.json`, the annotated `version:` line in
`plugin.yaml`, and `CHANGELOG.md`. Merging that PR creates the `vX.Y.Z` tag
and the GitHub Release. Because `hermes plugins install` clones `main` and
`hermes plugins update` is a `git pull`, **main is the release channel** —
keep it green.

## Release checklist

1. Confirm `main` is green and review the standing release PR's version and
   changelog.
2. **Pre-release E2E** (real Cassette account). Preferred: trigger the
   maintainer-only workflow, which runs the API-transport harness against a
   real edit job using the repository secrets:

   ```bash
   gh workflow run e2e.yml
   gh run watch
   ```

   Or run it locally (also covers the gateway-marked tests):

   ```bash
   RUN_CASSETTE_E2E=1 .venv/bin/python -m pytest -q -m e2e
   .venv/bin/python scripts/e2e_local_cassette.py --transport api \
     --media tests/fixtures/sample.mp4 \
     --instruction "Make a short captioned video under 10 seconds."
   ```

   Record the hermes-agent version you tested against and update the
   Requirements line in both READMEs if it changed.
3. Trigger CI on the release PR. Release PRs are opened with `GITHUB_TOKEN`,
   which does **not** trigger workflows — close and reopen the PR (or push an
   empty commit to its branch) so required checks run.
4. Merge the release PR. Verify the `vX.Y.Z` tag and GitHub Release exist and
   that `plugin.yaml` on `main` carries the new version.
5. Spot-check the official channel on a machine (or scratch `HERMES_HOME`):

   ```bash
   hermes plugins install Cassette-Editor/oh-my-cassette   # fresh install, or:
   hermes plugins update cassette                 # existing install
   hermes plugins list                            # shows the new version
   ```

6. Announce: Nous Discord `#plugins-skills-and-sands`, and keep the
   awesome-hermes-agent directory entries current.

If release-please proposes the wrong version, adjust with a
`Release-As: X.Y.Z` footer on an empty commit to `main`.

## One-time repository settings (owner/admin)

- **Actions → General → Workflow permissions**: enable *"Allow GitHub Actions
  to create and approve pull requests"* (release-please fails without it).
- **Pull Requests**: enable squash merge and *"Default to pull request
  title"* — release-please parses the squash commit message, so PR titles
  must be conventional commits (see CONTRIBUTING.md).
- **Branch protection on `main`**: require the `changes`, `test`,
  `install-smoke`, and `frontend` checks; disallow force pushes.
  (Path-skipped jobs count as satisfied.)
- **Code security**: enable secret scanning + push protection, Dependabot
  alerts, and CodeQL default setup (Python + JavaScript).
- **E2E secrets**: create the `CASSETTE_AUTH_EMAIL` and
  `CASSETTE_AUTH_PASSWORD` repository secrets (a dedicated test account is
  recommended) so the manual `E2E` workflow can run. Optionally set a
  `CASSETTE_URL` repository variable to pin the region.
