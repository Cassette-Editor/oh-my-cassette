# Security Policy

## Reporting a vulnerability

Please report vulnerabilities privately via
[GitHub security advisories](https://github.com/Cassette-Editor/oh-my-cassette/security/advisories/new)
rather than opening a public issue. We'll acknowledge reports as quickly as we
can and coordinate a fix and disclosure.

## Scope notes

- The plugin performs a scripted browser login to Cassette. Credentials
  (`CASSETTE_AUTH_EMAIL` / `CASSETTE_AUTH_PASSWORD`) live only in
  `~/.hermes/.env` on the operator's machine; they are never written to job
  metadata, logs, or this repository. `scripts/diagnose_install.py` redacts
  secrets from its output.
- Gateway platform credentials (QQ/Telegram/WeChat) are managed by Hermes
  Agent itself, not by this plugin.
- `JAMENDO_CLIENT_SECRET` is stored but never sent to Jamendo.

## Repository hygiene (for contributors)

Never commit:

- `.env` or `.env.e2e`;
- real gateway tokens, account IDs, chat IDs, or raw `wxid` values;
- Cassette credentials;
- Jamendo credentials;
- downloaded media, exports, job state, browser traces, or local runtime cache.

Runtime state belongs under `~/.hermes/cassette`, not in this repository.
Secret scanning and push protection are enabled on this repository.
