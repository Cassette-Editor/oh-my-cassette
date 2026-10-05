# Sign in to Cassette

```sh
oh-my-cassette login --target web
oh-my-cassette status
oh-my-cassette check
oh-my-cassette logout
```

`login` opens your browser. Enter an invited email address, paste the six-digit email code,
and approve the connection. The browser returns through a temporary loopback callback;
you never copy a token. `--no-browser` prints the authorization URL for a browser you choose.
Cancelling in the browser ends that login attempt. The bridge does not open repeated login windows.

The default Web resource is `https://trycassette.online/mcp`. A self-hosted or local installation
can select its published resource with `CASSETTE_MCP_URL` before running `login`.
The server must publish OAuth Protected Resource Metadata and its authorization service.
This branch requires the matching Cassette authorization-service deployment before public use.

To use local projects, open a local profile in Cassette Desktop, then run:

```sh
oh-my-cassette login --target desktop
oh-my-cassette status --target desktop
```

Desktop opens its local authorization page. Allow or deny the connection; no email code, cloud
account or Internet connection is needed. The grant binds the selected local profile, client and
device resource. Desktop publishes these identities and its loopback issuer in `mcp.json` in its
application-data directory. Tests and alternate installations can set `CASSETTE_DESKTOP_DISCOVERY`
to that file. A missing Desktop, a different profile or an old cloud grant requires an explicit new
authorization. Reopen the original profile to use its saved grant again.

Every grant allows Agent operations on all projects owned by that account at the selected target,
including creating, listing and switching projects. A full-access account still gives MCP only
Agent permissions. Project selections are separated by account, resource and working directory.

Credentials use macOS Keychain, Windows Credential Vault or Linux Secret Service. The latter must
be installed and unlocked on Linux; there is no plaintext fallback. Public connection metadata and
refresh locks live in `~/.config/oh-my-cassette` (`CASSETTE_AUTH_DIRECTORY` can isolate a test run).
Several hosts can share a grant safely: the lock covers reading, rotating and saving the refresh token.
The refresh token never appears in MCP configuration, logs or stdout.

The stdio server can start before login and exposes `cassette_bridge_status`. After login it retries
the connection and announces an updated tool list. Restart a host that does not support tool-list
updates. `logout --target web|desktop` revokes the grant at its issuer, then removes its local credential;
for Desktop this is a local request. If the service is unreachable, reopen it and retry logout.

`CASSETTE_AUTH_TOKEN` remains an advanced override for compatible credentials. It does not bypass
resource, current account permission or project ownership checks. Remove stale overrides before
using a saved OAuth login. Upload and download requests attach credentials only to the selected
resource's origin; external presigned URLs receive no Cassette credential.
