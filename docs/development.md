# Development & Troubleshooting

[← Back to README](../README.md)

> [!TIP]
> Join our Discord community to connect with contributors and fellow `oh-my-cassette` users.
>
> [![Discord](https://img.shields.io/discord/1514649803626250452?style=for-the-badge&logo=discord&logoColor=white&label=Discord&labelColor=black&color=5865F2)](https://discord.gg/qd9NY4k8d7)

## How it fits together

```text
host (Claude Code / Codex / OpenCode / Hermes)
  └─ stdio ─ uvx oh-my-cassette              src/oh_my_cassette/cli.py
               bridge/server.py                tools, instructions, calls and progress, cassette_bridge_status
               bridge/upstream.py              one MCP client session upstream, reconnects, 401 / unreachable
               bridge/files.py                 local-file policy and the upload handshake
               bridge/prepare.py               ffmpeg: video renditions, audio/image conversion, the report
               bridge/jobs.py                  the local phase in the background, `preparing`, attaching
               bridge/downloads.py             declared downloads into cassette-exports/
               contract.py                     the contract's markers and the `prepare` vocabulary
               conformance.py                  `oh-my-cassette check`
                        │ Streamable HTTP (MCP 2026-07-28) + bearer token
                        ▼
              Cassette MCP service             in Cassette-Editor (`mcp/`), default http://127.0.0.1:8790/mcp
                        │ public HTTP API with the caller's token
                        ▼
              Cassette-Editor backend
```

The bridge never names a backend tool. It learns which arguments are local paths, how to prepare
them and which results carry downloads from the `_meta` markers in [v3/contract.md](./v3/contract.md);
the design is in [v3/design.md](./v3/design.md).

A call with local files:

1. The bridge checks every path against the local policy (workspace or `CASSETTE_ALLOWED_ROOTS`,
   media types, size) and keys the call by tool, `prepare` digest, arguments and file identities.
2. A background task prepares the files (ffmpeg) and runs the upload handshake. The call waits at
   most `CASSETTE_LOCAL_WAIT_SEC`; if the task is still going it answers `{"status": "preparing"}`
   and the next identical call attaches to the same task.
3. With the refs in hand, the bridge forwards the call with `elapsedSeconds` in its `_meta`. The
   service waits a bounded time too and answers `running` / `processing` when its work outlives it.

## Running the local stack

In a Cassette-Editor checkout:

```bash
bun run dev:lambda      # web editor, API on :8787, worker
bun run dev:mcp         # the MCP service on 127.0.0.1:8790
```

Sign in to the local service with `CASSETTE_MCP_URL=http://127.0.0.1:8790/mcp uv run oh-my-cassette login --target web`. It must expose OAuth resource metadata; see [OAuth setup](oauth.md). Credentials go to the system keyring.

## Development

```bash
uv sync --group dev
uv run pytest -q -rs                               # reference service on loopback; needs ffmpeg for test_prepare
uv run ruff check . && uv run ruff format --check .
uv run oh-my-cassette check --url http://127.0.0.1:8790/mcp --upload
RUN_CASSETTE_LIVE=1 uv run pytest tests/live -q -rs          # the local stack, including a real export
uv build && uvx --from dist/*.whl oh-my-cassette --version
```

`tests/reference_remote.py` is an executable reference service for the contract: it requires a
bearer token, declares `prepare`, deduplicates by content and checks every PUT. `tests/test_prepare.py`
runs the real ffmpeg on small generated samples (HEVC, 10-bit PQ, rotation, odd sizes, VFR, timecode,
images, audio) and checks every rendition with ffprobe; it skips itself without ffmpeg. The live test
needs ffmpeg with libx265 and renders through the backend's configured provider, which may cost money.

Run the checkout inside a host:

```bash
claude mcp add cassette-dev -e OH_MY_CASSETTE_LOG=DEBUG \
  -- uv run --directory "$PWD" oh-my-cassette
```

The skill ships twice (`skills/cassette-video-edit/SKILL.md` and `.agents/skills/cassette-video-edit/SKILL.md`);
`tests/test_manifests.py` fails if the copies differ, if the skill names a backend tool, or if it
misses a `bridge.*` error code.

## Troubleshooting

| Result | Meaning | What to do |
|---|---|---|
| Only `cassette_bridge_status` is listed | The service is unreachable, refused the token, or needs a newer bridge. | Call it: `error_code` and `error` say which. |
| `bridge.unauthorized` | The service answered HTTP 401. | Run `oh-my-cassette login`; remove any stale explicit token override. |
| `bridge.backend_unreachable` during a call | The connection broke or went silent for 60 s. | Call again with the same arguments: the service's work may still be running. |
| `{"status": "preparing"}` | Local preparation or upload is still going. | Call again with the same arguments to keep waiting. |
| `bridge.ffmpeg_unavailable` | No ffmpeg / ffprobe on PATH. | Install ffmpeg, or set `CASSETTE_FFMPEG` and `CASSETTE_FFPROBE`. |
| `bridge.prepare_failed` | ffmpeg could not read or convert a file; the message ends with its stderr. | Check the file; convert it by hand if needed. |
| `bridge.contract_violation` | The service's markers are malformed (for example `prepare`). | Run `oh-my-cassette check` against it. |
| Connection refused | Nothing listens on `CASSETTE_MCP_URL`. | Start the MCP service; check the URL in the host config. |

Bridge logs go to stderr (`OH_MY_CASSETTE_LOG=DEBUG`); hosts show them in their MCP log view.

## Public repository safety

Do not commit `.env` files, tokens or passwords, service URLs other than the local defaults, media
beyond `tests/fixtures/`, or exports.
