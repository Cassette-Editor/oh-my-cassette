# Security policy

## Reporting a vulnerability

Report vulnerabilities privately through [GitHub security advisories](https://github.com/Cassette-Editor/oh-my-cassette/security/advisories/new), not a public issue. We will acknowledge the report and coordinate remediation and disclosure.

## Trust boundaries

Everything this repository ships runs on the user's machine and opens no port: every host (Claude
Code, Codex, OpenCode, Hermes) launches `uvx oh-my-cassette` as a local stdio child process.

The Cassette backend configured with `CASSETTE_API_URL` is a separate service that receives
credentials (if configured), media, editing requests, project state and render requests. Do not
treat the local process and the backend as the same service — a report about rendering, storage,
or account handling belongs to Cassette, not here.

## Credentials and local state

- No credential is required against a local stack started with `AGENT_AUTH_ENABLED=false`.
- `CASSETTE_AUTH_TOKEN` is used as a bearer token as-is. `CASSETTE_EMAIL`/`CASSETTE_PASSWORD` are
  exchanged once at `/api/agent-auth/verify`; the token is cached in
  `~/.oh-my-cassette/credentials.json` (mode 0600) and the password is never written to disk.
- `~/.oh-my-cassette/state.json` (mode 0600) holds project bindings and media maps only.
- Uploads go to presigned URLs issued by the backend; no other network destination exists in the code.

## Supported versions

Only the latest release on PyPI receives fixes.
