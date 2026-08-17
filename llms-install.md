# Installing Oh My Cassette — instructions for an AI assistant

This file is written for the agent doing the install (Cline, Claude Code, Codex, OpenCode, or any
other MCP host). Follow it top to bottom. Human-facing context lives in
[README.md](README.md); this file only covers setup.

## What you are installing

A **local stdio MCP server**. It is launched as a child process by the host, communicates over
stdin/stdout, and opens no port. Video editing and rendering happen on the separate hosted Cassette
backend (`https://trycassette.online`); this repository is the client, the safety boundary, and the
packaging. The server exposes 15 `cassette_*` tools, plus a `jamendo_music_matcher` helper (16 tools listed in total).

## Four facts that change how you install this

1. **It reads local files.** The user's clips stay on their machine and are uploaded by the plugin.
   Do not containerize it, do not host it remotely, and do not substitute a hosted MCP endpoint —
   any of those breaks media access.
2. **A Cassette account is required, and the password is server-generated.** Cassette emails it. You
   must never invent, guess, or "reset to" a password. See [Step 5](#step-5--sign-this-machine-in).
3. **First launch builds a locked virtualenv** — seconds with a warm pip cache, a few minutes cold.
   Pre-build it in [Step 3](#step-3--pre-build-the-runtime) so the host does not time out on the
   first tool call.
4. **Media access is root-scoped.** The server only opens files under roots the user has granted.
   A clip outside those roots fails with `source_path_not_allowed`, which is a configuration
   problem, not a bug. See [Granting access to media](#granting-access-to-media).

## Step 1 — check prerequisites

```bash
python3 --version                        # any version: this only launches the entrypoint
python3.13 --version || python3.12 --version || python3.11 --version   # the runtime needs one of these
git --version
ffmpeg -version                          # optional: only used for export thumbnails
```

The interpreter you put in the host config does **not** have to be 3.11–3.13 — it only runs the
launcher. The launcher then picks a supported interpreter for the runtime venv, searching
`CASSETTE_MCP_PYTHON`, itself, then `python3.13` / `python3.12` / `python3.11` on `PATH`. So a
machine whose `python3` is 3.14 installs fine as long as one supported version exists somewhere.

If none does, stop and tell the user which versions are acceptable — do not install a Python for
them silently. If a supported interpreter exists but is not on `PATH`, set `CASSETTE_MCP_PYTHON` to
its absolute path in the server's `env` block instead.

On Windows use `python` instead of `python3`.

## Step 2 — get the code

`release` is the stable channel; `main` is the development branch. Clone somewhere stable and
outside the user's project (the path goes into the host config, so it must not move):

```bash
git clone --branch release --depth 1 \
  https://github.com/Cassette-Editor/oh-my-cassette.git ~/.oh-my-cassette
```

The `release` branch is force-pushed on every release, so update it with a reset, never a merge:

```bash
cd ~/.oh-my-cassette && git fetch origin release && git reset --hard origin/release
```

## Step 3 — pre-build the runtime

```bash
python3 ~/.oh-my-cassette/scripts/run_local_mcp.py --bootstrap-only
```

This creates a virtualenv from the pinned lockfile and prints the interpreter path. It is
idempotent and keyed on the lockfile hash, so re-running it after an update is cheap. A failure
exits with status 2 and an explanation on stderr — read it before continuing.

## Step 4 — register the server with the host

Use an **absolute path**; most hosts do not expand `~`. Nothing else is required — the launcher
sets `CASSETTE_RUNTIME_ADAPTER=mcp` itself.

```json
{
  "mcpServers": {
    "cassette": {
      "command": "python3",
      "args": ["/Users/<you>/.oh-my-cassette/scripts/run_local_mcp.py"],
      "env": {
        "CASSETTE_MCP_HOST": "cline",
        "CASSETTE_PROJECT_ROOTS": "/Users/<you>/Movies:/Users/<you>/Desktop"
      }
    }
  }
}
```

- `CASSETTE_MCP_HOST` — free-form host identifier used for update hints; set it to your host's name.
- `CASSETTE_PROJECT_ROOTS` — `os.pathsep`-separated list (`:` on macOS/Linux, `;` on Windows) of
  directories whose media the user allows the plugin to read. Ask the user which directories their
  clips live in; do not guess, and do not grant their home directory wholesale.

Cline's settings file, if you are installing for Cline:

| OS | Path |
|---|---|
| macOS | `~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` |
| Linux | `~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` |
| Windows | `%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\settings\cline_mcp_settings.json` |

**Timeouts:** an editing turn legitimately runs for minutes. If your host has a hard per-tool
wall-clock cap, raise it (30 minutes is a safe value) — the server streams progress notifications,
but a hard cap ignores them and will kill a healthy run.

## Step 5 — sign this machine in

Cassette generates the password and emails it to the account address. The user never chooses one and
neither do you.

**Preferred — a private terminal.** This keeps the password out of the conversation transcript
entirely. Ask the user to run it themselves:

```bash
python3 ~/.oh-my-cassette/scripts/setup_local_mcp.py --email you@example.com
```

It prompts with `getpass`, verifies the account against Cassette before writing anything, and stores
the credentials with mode `0600` in a `0700` config directory.

**Alternative — in the conversation.** If the user prefers, they can paste the emailed password and
you call the `cassette_login` tool. State the trade-off first, plainly: the password will be written
to the host's transcript on disk and sent to the model provider for the rest of the conversation.
Let the user choose; do not pick this route for them because it is fewer steps.

**Never** call `cassette_login` with `request_new_password` on your own initiative. A reset is
irreversible, rate-limited, and replaces the account password on all of the user's other machines —
it needs an explicit, informed request from the user.

Credentials can also arrive as `CASSETTE_AUTH_EMAIL` / `CASSETTE_AUTH_PASSWORD` in the process
environment. Environment values outrank stored config, and `cassette_login` refuses to write a file
that the environment would shadow.

## Step 6 — verify

1. Restart the host so it relaunches the server.
2. Confirm the tool list arrives — 15 `cassette_*` tools plus `jamendo_music_matcher` — including
   `cassette_ingest_media`, `cassette_run_job`, and `cassette_login`.
3. Run the diagnostic — it prints configuration and reachability, never credentials:

   ```bash
   python3 ~/.oh-my-cassette/scripts/diagnose_local_mcp.py
   ```

4. Real check: call `cassette_ingest_media` on one small local video the user points you at. A
   successful call returns validated metadata (`asset_id`, `sha256`, `size_bytes`), a `session_id`,
   and `phase: "assets_ready"`. Do not declare the install finished before this succeeds.

Ingestion is local, so steps 1–4 all work **before** sign-in — run them even if Step 5 is still
pending. `cassette_run_job` is the first tool that needs credentials; without them it returns an
`auth_required` envelope carrying a `recovery` list and the exact `setup_command` for this install.

## Granting access to media

Allowed roots come from four places, all of which are additive:

- **MCP roots**, if your host advertises the `roots` capability — those are picked up automatically
  and nothing else is needed. Cline-style JSON configs do not advertise roots, so they need one of
  the options below. The server states which way it went on stderr at startup.
- `CASSETTE_PROJECT_ROOTS` / `CASSETTE_PROJECT_ROOT` in the server's environment (Step 4). This is
  the practical choice for a host-config install.
- `CASSETTE_ALLOWED_SOURCE_ROOTS`, same `os.pathsep`-separated format.
- `media_roots` in the protected `settings.json`, written as part of sign-in:

  ```bash
  python3 ~/.oh-my-cassette/scripts/setup_local_mcp.py \
    --allowed-root /Users/<you>/Movies --allowed-root /Users/<you>/Desktop
  ```

  Note that `--allowed-root` only works **inside the sign-in flow**: the script always asks for the
  email and password and re-verifies them against Cassette before writing anything, so it cannot add
  a root on its own. Pass every root in that one command — the flag **replaces** the stored list
  rather than appending to it — and make sure each directory already exists and is not a symlink.
  To add a root later without re-authenticating, use the environment variables above.

Related limits: `CASSETTE_ALLOWED_EXTENSIONS` (comma-separated) and `CASSETTE_MAX_BYTES`
(default 2 GiB per file).

## Optional — Jamendo music matching (BYOK)

Jamendo lookups need the user's own read-only Client ID from
<https://devportal.jamendo.com/> (no Client Secret). Either:

```bash
python3 ~/.oh-my-cassette/scripts/setup_local_mcp.py --jamendo
```

or have the user paste the ID and call `cassette_jamendo_setup`, which validates it before storing.
`JAMENDO_CLIENT_ID` in the environment takes precedence. BYOK grants API quota, not music licensing
rights — the returned track carries its own license URL and attribution requirements.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Server exits immediately, status 2 | Bootstrap failed. Re-run Step 3 and read stderr; usually an unsupported Python version. |
| `auth_required` envelope | Step 5 was skipped, or the environment shadows stored credentials. The envelope carries the exact setup command for this install. |
| `source_path_not_allowed` | The clip lives outside every granted root. See [Granting access to media](#granting-access-to-media). |
| Run is killed after a few minutes | Host tool timeout is too low. Raise it; progress notifications do not extend a hard cap. |
| Nothing on stdout but the protocol | Correct by design. All diagnostics go to stderr. |
| Tool call returns `thread_busy` | A run is already live on that session's thread. Wait for it and retry; do not open a second session for the same edit. |

## Operating it afterwards

- One `cassette_ingest_media` call per source file; reuse the returned `session_id`.
- Pass the user's editing request to `cassette_run_job` **verbatim**. Do not rewrite, expand, or
  "improve" it — the Cassette agent reads the session's media itself.
- Route on the typed `phase` / `next_action` fields, never on prose.
- Only pass `export=true` when the user has expressed finish/export intent.

## Uninstalling

Remove the server entry from the host config, delete the clone, and — only if the user asks to
forget this machine's credentials — remove the config and data directories:

| OS | Config | Data |
|---|---|---|
| macOS | `~/Library/Application Support/Oh My Cassette` | `~/Library/Application Support/Oh My Cassette/data` |
| Linux | `~/.config/oh-my-cassette` | `~/.local/share/oh-my-cassette` |
| Windows | `%APPDATA%\Oh My Cassette` | `%LOCALAPPDATA%\Oh My Cassette\data` |

`setup_local_mcp.py --logout` forgets the stored password without touching the Cassette account.
