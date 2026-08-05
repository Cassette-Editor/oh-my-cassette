# Oh My Cassette installed 🎬

Finish setup (shared MCP configuration, ffmpeg detection, region choice):

    python3 ~/.hermes/plugins/cassette/scripts/install_plugin.py --setup-only

This writes the same local stdio MCP server used by Codex, Claude Code, and OpenCode into
`~/.hermes/config.yaml` with an 1800-second tool timeout. Then enable the thin gateway plugin and
restart the gateway:

    hermes plugins enable cassette
    hermes gateway restart

Notes:

- Configuration lives in `~/.hermes/.env`. A `.env` file inside the plugin
  directory is just an unused copy of `.env.example` — you can ignore it.
- Verify your install anytime:
  `python3 ~/.hermes/plugins/cassette/scripts/diagnose_install.py`
- Docs: https://github.com/Cassette-Editor/oh-my-cassette#-quick-start
