<p align="center">
  <img src="assets/banner.jpg" width="80%" alt="Oh My Cassette banner" />
</p>

<h1 align="center">Oh My Cassette</h1>

<p align="center">
  在 Codex、Claude、Hermes 或 Web Demo 里，用对话完成视频剪辑。
</p>

<p align="center">
  <b>简体中文</b> · <a href="./README.md">English</a> ·
  <a href="https://trycassette.online/">Cassette</a> ·
  <a href="https://github.com/Cassette-Editor/oh-my-cassette/releases">版本发布</a>
</p>

Oh My Cassette 把本地或网关中的媒体文件与自然语言需求，转换成一个可引导、可恢复、可审查的 Cassette 剪辑任务。Cassette 仍然是实际的剪辑引擎；本仓库负责宿主集成、安全导入、任务状态、后台监控、完成审查和导出校验。

## 四种入口，共用同一套剪辑核心

| 入口 | 运行方式 | 仍然独立的部分 |
|---|---|---|
| Codex 插件 | 本地 `cassette` stdio MCP 子进程 | 不启动本地 HTTP 服务 |
| Claude 插件 | 同一个本地 `cassette` stdio MCP 子进程 | 不启动本地 HTTP 服务 |
| Hermes 插件 | 保留现有工具、Hook、命令、网关回传和 Hermes 专用 Skill | 继续使用 `~/.hermes/.env` 和网关目录 |
| Web Demo | 保留 FastAPI 上传/聊天服务与 React 前端 | 只服务于 Demo |

这里的 **MCP server** 指由宿主通过标准输入/输出连接的本地子进程。它不监听 TCP 端口，也不依赖 FastAPI Demo 服务。独立的 Cassette 后端仍负责鉴权、媒体处理、Agent 运行、项目状态和渲染；本仓库不会移除或打包该后端。

Web Demo 之所以继续保留服务端，是因为浏览器仍需要上传和应用接口。原生插件去掉这层服务，不等于删除 Demo，也不等于删除 Cassette 后端。

## 环境要求

- macOS 或 Linux；
- Python 3.11–3.13；
- Cassette 账号（[注册](https://trycassette.online/signup/)）；
- 默认 API 传输需要完整 Cassette API 权限；没有该权限时可选装 Playwright/Chromium；
- Hermes 网关视频转码及可选 API 导出缩略图需要 `ffmpeg`。

本地启动器会自动创建并更新带锁定依赖的插件专用虚拟环境。Codex 和 Claude 不需要 FastAPI、uvicorn，也不需要另起一个常驻服务。

## Codex 安装

添加仓库 Marketplace 并安装插件：

```bash
codex plugin marketplace add https://github.com/Cassette-Editor/oh-my-cassette.git
codex plugin add oh-my-cassette@cassette-editor
```

安装后新建一个 Codex 任务。插件会提供一个宿主无关的视频剪辑 Skill，以及一个名为 `cassette` 的本地 MCP 服务。

## Claude 安装

把同一个仓库添加为 Claude Marketplace：

```bash
claude plugin marketplace add Cassette-Editor/oh-my-cassette
claude plugin install oh-my-cassette@cassette-editor
```

安装后重启 Claude Code。运行 `claude plugin details oh-my-cassette@cassette-editor`，应当看到 1 个 Skill 和 1 个 MCP server。

## 首次鉴权

Codex 与 Claude 共用一份受保护的鉴权和数据目录，但与 Hermes 隔离。缺少凭据不会导致 MCP 初始化失败；只有确实需要连接 Cassette 的工具会返回 `auth_required`，其中包含一条可在私密终端直接执行的精确命令。

在仓库检出目录中，对应命令为：

```bash
python3 scripts/setup_local_mcp.py
```

脚本通过 `getpass` 隐藏密码输入，先调用 `/api/agent-auth/verify` 校验，成功后才原子写入本地文件。Access token 和 refresh token 只保存在内存中，绝不会落盘。

常用选项：

```bash
# 信任当前项目以外的媒体目录。
python3 scripts/setup_local_mcp.py --allowed-root /absolute/path/to/media

# 明确地一次性导入现有 Hermes Cassette 凭据。
python3 scripts/setup_local_mcp.py --import-hermes

# 安装锁定版本的 Playwright + Chromium，并选择浏览器传输。
python3 scripts/setup_local_mcp.py --with-browser
```

受保护目录的位置：

| 平台 | 配置 | 共享数据和导出文件 |
|---|---|---|
| macOS | `~/Library/Application Support/Oh My Cassette/` | `~/Library/Application Support/Oh My Cassette/data/` |
| Linux | `${XDG_CONFIG_HOME:-~/.config}/oh-my-cassette/` | `${XDG_DATA_HOME:-~/.local/share}/oh-my-cassette/` |

配置目录权限为 `0700`，凭据文件为 `0600`。程序会拒绝符号链接或权限过宽的凭据文件。读取顺序是：进程环境变量优先，其次是受保护的本地配置。Hermes `.env` 只能显式导入，不是原生插件的运行依赖。

如果校验结果表明账号没有完整 API 权限，脚本会记录该状态，MCP 工具会返回可选浏览器安装命令，而不会悄悄切换传输方式。

## 在 Codex 或 Claude 中剪辑

直接要求宿主剪辑项目内的文件，例如：

> 把 `media/trip.mp4` 剪成 20 秒竖屏高光，添加简短中文字幕和舒缓的纯音乐。

宿主无关的 Skill 会引导以下流程：

1. 导入每个可信媒体路径。第一次调用会生成加密随机的 `session_id`。
2. 确认素材，并只询问尚未明确的模型、思考等级、提示词优化和 BGM 选择。
3. 构建 Cassette 提示词，默认以后台任务启动。
4. 使用最长 30 秒的有界长轮询监控状态。
5. Cassette 真正需要用户选择时，用同一个 `job_id` 和用户回答恢复任务。
6. 审查完成状态；只有显式且校验通过的 `export` 决策才会开始渲染。
7. 返回经过校验的绝对路径、文件 URI、MIME、字节大小和 MCP resource link。

流程以结构化的 `phase` 和 `next_action` 为准，不根据自然语言关键词猜测状态。默认监控预算是 `CASSETTE_MCP_MONITOR_BUDGET_SEC=1500`。预算到期后如果任务仍在运行，宿主会返回有效的任务 ID，而不是丢失任务。

### 会话、重启与交接

Codex 与 Claude 共用安全配置和数据目录，但新会话默认互相隔离。需要主动交接时，把准确的 `session_id` 和 `job_id` 提供给另一个宿主；接收方应先调用 `cassette_job_status`，不要重复导入或创建任务。

API 任务会持久化私有 LangGraph thread/interrupt 元数据，因此因用户问题暂停的任务可以在宿主重启后继续。浏览器传输的页面对象只存在于当前 MCP 进程；进程重启后会返回 `browser_session_lost`，用户可重新开始浏览器任务。

导出文件保留在共享数据目录：

```text
<data-root>/cassette/exports/<job_id>/
```

只有位于对应任务导出目录、且真实存在的文件才会返回。MCP 不会把大型媒体字节嵌入响应，也不会暴露任意本地文件。

## MCP 工具

原生插件与 Hermes 保持完全相同的 11 个工具名：

| 工具 | 用途 |
|---|---|
| `cassette_ingest_media` | 把可信项目媒体安全复制到隔离会话 |
| `cassette_list_assets` | 读取会话素材清单 |
| `cassette_make_prompt` | 构建完整 Cassette 剪辑请求 |
| `cassette_match_bgm` | 匹配 Free To Use BGM |
| `cassette_match_exact_bgm` | 按明确歌曲名和歌手匹配 |
| `jamendo_music_matcher` | 按固定字段匹配 Jamendo 音乐 |
| `cassette_answer_question` | 分类问题，或恢复等待用户输入的任务 |
| `cassette_run_job` | 开始剪辑；MCP 默认后台运行 |
| `cassette_job_status` | 查询状态，或最长长轮询 30 秒 |
| `cassette_review_completion` | 校验完成状态，并显式批准导出 |
| `cassette_cancel_job` | 请求协作式取消 |

所有 MCP 结果都使用结构化 envelope，包含 `ok`、类型化 `data` 或 `error`、`session_id`、`job_id`、`phase`、`next_action` 和校验后的 artifacts。

## 两种传输方式

### API（默认）

API 传输直接连接 Cassette 后端：鉴权、上传媒体、等待衍生文件、运行 Cassette LangGraph Agent、处理类型化 interrupt，再渲染保存的项目。遇到 `401` 只重新鉴权并重试一次，token 始终只在内存中。

用于恢复任务的 API 元数据会私密地随任务持久化。编辑完成后默认进入 `review_required`；只有 `cassette_review_completion(decision="export")` 才会启动渲染。

### 浏览器（可选）

浏览器传输用锁定版本的 Playwright/Chromium 驱动 Cassette Agent 页面。需要时显式安装：

```bash
python3 scripts/setup_local_mcp.py --with-browser
```

它适合没有完整 API 权限的账号，也可用于传输一致性诊断。只要同一个 MCP 进程还保留浏览器会话，问题暂停就可以继续；无法跨进程重启恢复是唯一明确保留的限制。

## Hermes

现有 Hermes 插件继续完整支持 11 个工具、网关 Hook、命令、回传通知器和 Hermes 专用 Skill。

```bash
hermes plugins install Cassette-Editor/oh-my-cassette
python3 ~/.hermes/plugins/cassette/scripts/install_plugin.py --setup-only
hermes plugins enable cassette
hermes gateway restart
```

Hermes 继续使用 `~/.hermes/.env`、`~/.hermes/cassette` 以及 QQ/Telegram/Weixin 缓存目录。原生 MCP 配置不会改变这些默认值。

典型网关流程：

1. 发送视频、图片或音频素材；
2. 在同一会话发送剪辑需求；
3. 首次剪辑选择模型/思考等级、提示词优化和智能 BGM；
4. 后台任务通过保存的网关目标回传进度和导出视频。

常用命令包括 `/edit`、`/refine`、`/music`、`/cut`、`/check_assets`、`/cassette_model`、`/cassette status <job_id>` 和 `/cassette cancel <job_id>`。

更新 Hermes：

```bash
hermes plugins update cassette
hermes gateway restart
```

## Web Demo

FastAPI 服务端被**明确保留，只用于 Web Demo**。它继续处理浏览器上传、聊天/会话接口、前端资源和部署行为；本地 MCP 完全不依赖它。Demo 仍然连接独立的 Cassette 后端。

公开 Demo：[http://43.134.224.156:8080/](http://43.134.224.156:8080/)

> [!WARNING]
> 公开 Demo 没有鉴权，只适合评估。请勿上传敏感、私密、受监管、违法或受限制的内容。素材与提示词可能由 Demo 运营方、Cassette、所配置的 LLM 和音乐服务处理。

本地运行 Demo：

```bash
python3 -m venv .venv-web
. .venv-web/bin/activate
pip install -r requirements-web.txt
python -m playwright install chromium
cp deploy/oh-my-cassette-web.env.example ./oh-my-cassette-web.env
# 编辑进程环境变量，然后：
set -a; . ./oh-my-cassette-web.env; set +a
./web_demo/build_frontend.sh
python -m web_demo.server
```

打开 `http://127.0.0.1:8080/`。Demo 只读取自身进程环境，不读取原生插件安全配置，也不会隐式导入 Hermes `.env`。systemd 示例仍位于 `deploy/`。

## 更新

Codex：

```bash
codex plugin marketplace upgrade cassette-editor
codex plugin add oh-my-cassette@cassette-editor
```

Claude：

```bash
claude plugin marketplace update cassette-editor
claude plugin update oh-my-cassette@cassette-editor
```

启动器会对 `requirements-mcp.lock` 计算哈希，并在下次启动时更新插件专用环境。只有用户明确请求时才安装浏览器组件。

## 诊断与排障

原生插件诊断不会输出凭据：

```bash
python3 scripts/diagnose_local_mcp.py
```

Hermes 使用独立诊断：

```bash
python3 scripts/diagnose_install.py
```

常见问题：

- `auth_required`：在私密终端执行 `error.details.setup_command` 中的精确命令。
- `api_access_unavailable` 或 API `forbidden`：使用 `--with-browser`，或更换有完整 API 权限的账号。
- `source_path_not_allowed`：把素材放进当前项目，或用 `--allowed-root` 添加绝对可信目录。
- `browser_session_lost`：重新开始任务；需要跨重启恢复问题时应使用 API 传输。
- 任务一直是 `running`：保留任务 ID，稍后查询；不要高频轮询。
- 没有导出 artifact：不要猜测路径，应检查结构化错误和完成审查状态。
- 安装或更新后看不到 MCP：新建 Codex 任务，或重启 Claude 触发插件重新发现。

## 开发

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python \
  -r requirements-web.txt -r requirements-browser.lock pytest pytest-xdist
.venv/bin/python -m playwright install chromium

.venv/bin/python -m compileall -q .
.venv/bin/python -m pytest -q -rs -n 4 --dist loadfile
./web_demo/build_frontend.sh
```

开发时可跳过托管环境重建，直接运行真实 MCP 进程：

```bash
CASSETTE_MCP_SKIP_BOOTSTRAP=1 \
CASSETTE_MCP_PYTHON="$PWD/.venv/bin/python" \
.venv/bin/python scripts/run_local_mcp.py
```

维护者使用临时环境变量执行真实验收：

```bash
.venv/bin/python scripts/e2e_local_mcp.py \
  --host codex --transport api \
  --media /absolute/path/to/test.mp4 \
  --instruction "剪成一个带字幕的短视频。"
```

PR CI 不使用凭据，并覆盖 Ubuntu Python 3.11/3.13、macOS MCP 启动器与协议 Smoke、两种原生 CLI 安装、完整 Hermes/Web 测试和前端构建。手动 E2E 工作流才会通过仓库 Secrets 运行真实 MCP 剪辑。第三方 BGM 实时端点只作为可选诊断；PR CI 用确定性 fixture 覆盖所有工具与回退顺序。

更多信息见 [CONTRIBUTING.md](./CONTRIBUTING.md)、[RELEASING.md](./RELEASING.md)、[SECURITY.md](./SECURITY.md) 和 [CHANGELOG.md](./CHANGELOG.md)。

## 许可证

MIT，见 [LICENSE](./LICENSE)。
