# Oh My Cassette installed 🎬

Finish setup (Playwright Chromium in the Hermes venv, ffmpeg detection, region choice):

    python3 ~/.hermes/plugins/cassette/scripts/install_plugin.py --setup-only

Then enable the plugin and restart the gateway:

    hermes plugins enable cassette
    hermes gateway restart

Notes:

- Configuration lives in `~/.hermes/.env`. A `.env` file inside the plugin
  directory is just an unused copy of `.env.example` — you can ignore it.
- Verify your install anytime:
  `python3 ~/.hermes/plugins/cassette/scripts/diagnose_install.py`
- Docs: https://github.com/Cassette-Editor/oh-my-cassette#-quick-start
