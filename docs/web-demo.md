# Web Demo — Local Deployment

[← Back to README](../README.md)


The web demo is a single FastAPI service that keeps the existing Cassette gateway flow but replaces the chat surface with a browser UI. It stores runtime uploads, jobs, exports, screenshots, and outbox data under `CASSETTE_ASSET_ROOT` instead of the repository.

1. Clone the repository and create a dedicated web environment:

```bash
git clone https://github.com/Cassette-Editor/oh-my-cassette.git
cd oh-my-cassette

python3 -m venv .venv-web
. .venv-web/bin/activate
pip install -U pip
pip install -r requirements-web.txt
python -m playwright install chromium
```

2. Configure Cassette and DeepSeek runtime settings as process environment variables. The web demo does not require Hermes Agent or `~/.hermes/.env`.

```bash
cp deploy/oh-my-cassette-web.env.example ./oh-my-cassette-web.env
$EDITOR ./oh-my-cassette-web.env
```

At minimum, set:

```dotenv
CASSETTE_URL=https://sg.trycassette.online/agent
CASSETTE_AUTH_EMAIL=you@example.com
CASSETTE_AUTH_PASSWORD='your-cassette-password'
CASSETTE_ASSET_ROOT=$HOME/.oh-my-cassette/cassette
CASSETTE_BROWSER_TIMEOUT_SEC=1800

DEEPSEEK_API_KEY='your_deepseek_api_key'
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
OMC_WEB_HOST=0.0.0.0
OMC_WEB_PORT=8080
OMC_WEB_LOG_DIR=./web_demo/logs
```

If you load the file with `. ./oh-my-cassette-web.env`, quote any value that contains shell-special characters, spaces, or `#`. The systemd `EnvironmentFile` format also accepts quoted values.
Cassette upload/analysis waits inherit `CASSETTE_BROWSER_TIMEOUT_SEC` by default. Set `CASSETTE_UPLOAD_TIMEOUT_SEC` only when you need a different upload timeout; `0` means wait forever.

Load the variables before starting the service:

```bash
set -a
. ./oh-my-cassette-web.env
set +a
```

If you do not want to put a DeepSeek key on the server, leave `DEEPSEEK_API_KEY` empty and enter a temporary key from the web UI **Settings** panel. That browser-provided key is attached to API requests for that session and is not persisted by the web app.

Web demo service logs are separate from Hermes Agent / OhMyCassette plugin logs. By default they are written to `./web_demo/logs/web_demo.log` relative to the service working directory, or to `$OMC_WEB_LOG_DIR/web_demo.log` when `OMC_WEB_LOG_DIR` is set.
Cassette job records for the web demo are also separate when `CASSETTE_ASSET_ROOT` is set as above: raw job JSON lives in `$CASSETTE_ASSET_ROOT/jobs/cassette_*.json`, and the web UI exposes a per-job **Log** link for jobs owned by the current browser session.

3. Build the browser UI (Vite + React → `web_demo/frontend/dist`). Requires Node.js + npm at build time only:

```bash
./web_demo/build_frontend.sh
```

4. Start the web demo:

```bash
. .venv-web/bin/activate
python -m web_demo.server
```

> The server serves the built `web_demo/frontend/dist`. If it has not been built yet, `/` returns a clear 503 telling you to run the build. Re-run `web_demo/build_frontend.sh` after pulling changes under `web_demo/frontend`.

Open `http://127.0.0.1:8080/` for local testing, or `http://<server-ip>:8080/` from another device if the host firewall/security group allows inbound TCP `8080`.

4. Optional systemd deployment:

```bash
sudo cp deploy/oh-my-cassette-web.service.example /etc/systemd/system/oh-my-cassette-web.service
sudo cp deploy/oh-my-cassette-web.env.example /etc/oh-my-cassette-web.env
sudo $EDITOR /etc/oh-my-cassette-web.env
sudo systemctl daemon-reload
sudo systemctl enable --now oh-my-cassette-web
sudo systemctl status oh-my-cassette-web
journalctl -u oh-my-cassette-web -f
```

Before enabling the service, edit `/etc/systemd/system/oh-my-cassette-web.service` if your repository or virtualenv path differs from the example.
`journalctl` shows uvicorn stdout/access logs; Web demo business logs still go to `$OMC_WEB_LOG_DIR/web_demo.log`.

5. Smoke test:

```bash
curl -fsS http://127.0.0.1:8080/ -o /dev/null
python3 -m compileall -q web_demo tools.py notifier.py browser.py
tail -f ./web_demo/logs/web_demo.log
```

Then open the browser UI, upload a small video, send an edit request, and watch the job card/events until the export download appears.
