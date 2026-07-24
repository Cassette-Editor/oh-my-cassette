# 开发与排障

[← 返回 README](../README.zh-cn.md)

> [!TIP]
> 加入我们的Discord社区和`oh-my-cassette` 用户一起交流
>
> [![Discord](https://img.shields.io/discord/1514649803626250452?style=for-the-badge&logo=discord&logoColor=white&label=Discord&labelColor=black&color=5865F2)](https://discord.gg/qd9NY4k8d7)

## 常见问答

### 1. 为什么 Hermes Agent 在 QQ 或 Telegram 中没有响应？

请检查 Hermes Agent 的模型配置、网络连接和 API 连通性。你也可以重启网关：

```bash
hermes gateway stop
hermes gateway restart
```

### 2. 为什么连接 Cassette 时出现网络问题？

请检查是否可以访问 https://sg.trycassette.online/agent 或 https://trycassette.online/agent 。如果无法访问，请检查网络设置，确保可以打开 Cassette。

### 3. 为什么运行速度比较慢？

剪辑过程取决于 Hermes Agent 的 API 延迟和 Cassette 服务负载。根据任务复杂度、所选模型和思考等级不同，一次剪辑任务大约需要 5-20 分钟。

如果 Hermes 或 Cassette 卡住，可以先发送 `/cut` 停止当前 Cassette 剪辑，再发送 `/stop` 停止 Hermes。之后可以在同一个会话中重试。

## 诊断

诊断 Codex 与 Claude 本地 MCP 插件：

```bash
python3 scripts/diagnose_local_mcp.py
```

它会检查运行时引导、受保护配置、传输方式、项目与媒体根目录，以及宿主无关的数据路径，并且不会输出凭据。常见 MCP 错误都带有可执行的说明：`auth_required` 会提供私人设置命令，`source_path_not_allowed` 会指出可信目录问题，`browser_session_lost` 会说明浏览器任务为何无法跨重启继续。

诊断 Hermes：

```bash
python3 scripts/diagnose_install.py
```

诊断项包括：

- 插件安装路径（符号链接安装和 `hermes plugins install` 的 git 克隆安装都能识别）；
- 插件是否已在 Hermes 中启用；
- `~/.hermes/.env` 中的配置值，并隐藏敏感信息；
- `ffmpeg` 和 `ffprobe`；
- Hermes Python 环境中的 Playwright；
- Cassette 地址是否可访问；
- 通过 Chromium 打开 Agent 页面检查 Cassette 登录凭据；
- Hermes 网关状态。

如果接收媒体时报错 `transcoder_missing`，请重新运行安装器，让它记录明确的 `CASSETTE_FFMPEG_BIN` 和 `CASSETTE_FFPROBE_BIN` 路径：

```bash
python3 scripts/install_plugin.py \
  --skip-plugin-enable \
  --skip-cassette-url \
  --skip-cassette-auth \
  --skip-jamendo-auth \
  --skip-playwright-install
```

## 配置

Codex 与 Claude 共用操作系统标准的 Oh My Cassette 配置和数据目录，其凭据与任务状态和 Hermes 相互独立。当前宿主项目会自动加入可信范围；其他媒体目录必须通过 `setup_local_mcp.py --allowed-root` 显式添加。网页演示只读取自身进程环境，不会使用任一插件保存的凭据。

安装器会把常规运行时设置写入 `~/.hermes/.env`。你也可以手动编辑该文件。


最小可用配置示例：

```bash
CASSETTE_URL=https://sg.trycassette.online/agent
CASSETTE_AUTH_EMAIL=you@example.com
CASSETTE_AUTH_PASSWORD=your-generated-cassette-password
CASSETTE_ASSET_ROOT=$HOME/.hermes/cassette
CASSETTE_HEADLESS=true
CASSETTE_FORCE_H264=true
```

默认媒体来源目录：

```text
~/.hermes/qqbot
~/.hermes/telegram
~/.hermes/weixin
~/.hermes/cache
~/.hermes/tmp
```

如果你的网关把媒体保存在其他位置：

```bash
CASSETTE_ALLOWED_SOURCE_ROOTS="$HOME/.hermes/qqbot:$HOME/.hermes/telegram:$HOME/.hermes/cache:$HOME/.hermes/tmp:/path/to/media"
```

可选的 Jamendo 智能配乐配置：

```bash
JAMENDO_CLIENT_ID=your_client_id
JAMENDO_CLIENT_SECRET=your_client_secret
```

`JAMENDO_CLIENT_SECRET` 是为未来功能预留的字段。它不会被发送给 Jamendo，也不会写入任务元数据。

## 开发


创建本地测试环境：

```bash
uv venv .venv
uv pip install --python .venv/bin/python pytest playwright
.venv/bin/python -m playwright install chromium
```

运行检查：

```bash
python3 -m compileall -q .
.venv/bin/python -m pytest -q
```

使用开发环境运行真实的 stdio MCP 进程：

```bash
CASSETTE_MCP_SKIP_BOOTSTRAP=1 \
CASSETTE_MCP_PYTHON="$PWD/.venv/bin/python" \
.venv/bin/python scripts/run_local_mcp.py
```

确定性测试会覆盖核心能力对齐、全部 11 个工具、真实 stdio 协议调用、长轮询、重启与续跑、状态转换、资源链接、身份验证和文件系统安全、两种插件清单、现有 Hermes 与网页演示测试，以及前端构建。由维护者手动触发的实时 E2E 会通过临时环境变量读取仓库 Secret；PR CI 本身不使用凭据。

运行本地 Cassette 端到端测试工具：

```bash
.venv/bin/python scripts/e2e_local_cassette.py \
  --media tests/fixtures/sample.mp4 \
  --instruction "制作一个 10 秒以内、带字幕的短视频。"
```

运行网页演示服务：

```bash
uv venv .venv-web
uv pip install --python .venv-web/bin/python -r requirements-web.txt
.venv-web/bin/python -m playwright install chromium
# 构建浏览器前端（Vite/React -> web_demo/frontend/dist）；需要 Node.js + npm。
./web_demo/build_frontend.sh
set -a
. ./oh-my-cassette-web.env
set +a
.venv-web/bin/python -m web_demo.server
```

浏览器前端是位于 `web_demo/frontend` 的 Vite + React + TypeScript 应用；`web_demo/build_frontend.sh` 会将其编译到 `web_demo/frontend/dist`，由服务端在 `/static` 下提供。构建产物不会提交到仓库，因此每次部署都需要构建（拉取前端改动后也要重新构建）。如需实时调试前端，可运行 `cd web_demo/frontend && npm run dev`，Vite 会把 `/api` 代理到 `http://127.0.0.1:8088`，请同时运行 FastAPI 服务。

网页演示会从进程环境变量读取 `CASSETTE_*`、`DEEPSEEK_*` 和 `OMC_WEB_*`。浏览器内也可以在“设置”里临时填写 DeepSeek API Key；该 key 只随请求发送到当前服务器，不会写入仓库或服务端磁盘。示例 systemd 文件在 `deploy/oh-my-cassette-web.service.example`，环境变量模板在 `deploy/oh-my-cassette-web.env.example`。

真实网关端到端测试是可选项，默认会跳过：

```bash
RUN_CASSETTE_E2E=1 .venv/bin/python -m pytest -q -m e2e
```

## 公共仓库安全

请不要提交：

- `.env` 或 `.env.e2e`；
- 真实网关令牌、账号 ID、聊天 ID 或原始 `wxid`；
- Cassette 凭据；
- Jamendo 凭据；
- 下载的媒体、导出文件、任务状态、浏览器追踪记录或本地运行时缓存。

Hermes 运行时状态应保存在 `~/.hermes/cassette`；Codex 与 Claude 的状态应保存在操作系统标准的 Oh My Cassette 数据目录。它们都不应写进这个仓库。
