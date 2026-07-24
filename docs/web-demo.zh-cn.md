# Web Demo — 本地部署

[← 返回 README](../README.zh-cn.md)

Web Demo 是一个单进程 FastAPI 服务：它保留 Oh My Cassette 现有 gateway 流程，只把用户入口换成浏览器网页。上传素材、任务状态、导出文件、截图和网页 outbox 等运行时数据会写入 `CASSETTE_ASSET_ROOT`，不会写入仓库。

1. 克隆仓库并创建独立的网页演示环境：

```bash
git clone https://github.com/Cassette-Editor/oh-my-cassette.git
cd oh-my-cassette

python3 -m venv .venv-web
. .venv-web/bin/activate
pip install -U pip
pip install -r requirements-web.txt
python -m playwright install chromium
```

2. 通过进程环境变量配置 Cassette 和 DeepSeek。Web Demo 不要求安装 Hermes Agent，也不要求存在 `~/.hermes/.env`：

```bash
cp deploy/oh-my-cassette-web.env.example ./oh-my-cassette-web.env
$EDITOR ./oh-my-cassette-web.env
```

至少需要设置：

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

如果用 `. ./oh-my-cassette-web.env` 加载配置，包含 shell 特殊字符、空格或 `#` 的值请加引号；systemd 的 `EnvironmentFile` 同样支持带引号的值。
Cassette 上传/分析等待默认继承 `CASSETTE_BROWSER_TIMEOUT_SEC`。只有需要单独设置上传超时时才配置 `CASSETTE_UPLOAD_TIMEOUT_SEC`；设为 `0` 表示无限等待。

启动前加载这些变量：

```bash
set -a
. ./oh-my-cassette-web.env
set +a
```

如果不希望在服务器上保存 DeepSeek API Key，可以把 `DEEPSEEK_API_KEY` 留空，然后在网页 **设置** 面板里临时填写自己的 key。浏览器提供的 key 只会附加在当前会话的请求上，Web 应用不会持久化保存。

Web Demo 服务日志和 Hermes Agent / OhMyCassette 插件日志是分开的。默认写入服务工作目录下的 `./web_demo/logs/web_demo.log`；如果设置了 `OMC_WEB_LOG_DIR`，则写入 `$OMC_WEB_LOG_DIR/web_demo.log`。
如果按上面的方式设置了 `CASSETTE_ASSET_ROOT`，Web Demo 的 Cassette job 记录也会和 Hermes 分开：原始 job JSON 位于 `$CASSETTE_ASSET_ROOT/jobs/cassette_*.json`，网页任务卡片会为当前浏览器会话拥有的任务提供 **日志** 链接。

3. 构建浏览器前端（Vite + React → `web_demo/frontend/dist`），仅在构建时需要 Node.js + npm：

```bash
./web_demo/build_frontend.sh
```

4. 启动网页演示：

```bash
. .venv-web/bin/activate
python -m web_demo.server
```

> 服务端只提供构建产物 `web_demo/frontend/dist`。如果尚未构建，访问 `/` 会返回明确的 503 并提示先执行构建。拉取 `web_demo/frontend` 下的改动后，请重新运行 `web_demo/build_frontend.sh`。

本机测试打开 `http://127.0.0.1:8080/`；如果要让手机或其他电脑访问，请打开 `http://<服务器 IP>:8080/`，并确认服务器防火墙或云安全组允许入站 TCP `8080`。

4. 可选：使用 systemd 常驻运行：

```bash
sudo cp deploy/oh-my-cassette-web.service.example /etc/systemd/system/oh-my-cassette-web.service
sudo cp deploy/oh-my-cassette-web.env.example /etc/oh-my-cassette-web.env
sudo $EDITOR /etc/oh-my-cassette-web.env
sudo systemctl daemon-reload
sudo systemctl enable --now oh-my-cassette-web
sudo systemctl status oh-my-cassette-web
journalctl -u oh-my-cassette-web -f
```

启用前请检查 `/etc/systemd/system/oh-my-cassette-web.service`，如果你的仓库路径或虚拟环境路径与示例不同，需要先改成自己的实际路径。
`journalctl` 用来看 uvicorn 标准输出和 access log；Web Demo 业务日志仍写到 `$OMC_WEB_LOG_DIR/web_demo.log`。

5. 简单验收：

```bash
curl -fsS http://127.0.0.1:8080/ -o /dev/null
python3 -m compileall -q web_demo tools.py notifier.py browser.py
tail -f ./web_demo/logs/web_demo.log
```

然后在浏览器中上传一个小的视频文件，发送剪辑指令，观察事件流和任务卡片，直到出现导出下载入口。
