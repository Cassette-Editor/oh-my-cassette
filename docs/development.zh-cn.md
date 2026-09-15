# 开发与排障

[← 返回 README](../README.zh-cn.md)

> [!TIP]
> 加入我们的 Discord 社区和 `oh-my-cassette` 用户一起交流
>
> [![Discord](https://img.shields.io/discord/1514649803626250452?style=for-the-badge&logo=discord&logoColor=white&label=Discord&labelColor=black&color=5865F2)](https://discord.gg/qd9NY4k8d7)

## 整体结构

```text
宿主 (Claude Code / Codex / OpenCode / Hermes)
  └─ stdio ─ uvx oh-my-cassette            src/oh_my_cassette/server.py   9 个工具 + instructions
               ├─ tools/                     project · media · run · timeline · export
               ├─ app.py                     组装：settings、state、HTTP、各 client
               ├─ cassette/                  HTTP + SSE 客户端，后端契约的 pydantic 镜像
               │    projects · agent · media · export · prepare (ffmpeg) · auth
               ├─ render/                    时间线摘要 / delta，本地 contact sheet
               └─ state.py                   ~/.oh-my-cassette/state.json
                        │ HTTP + SSE
                        ▼
              Cassette-Editor 后端           /api/projects · /api/agent · /api/media · /api/export
```

一次剪辑 turn（`cassette_run`）：

1. 解析项目：显式 `project_id` → 当前目录的绑定 → 最近使用的项目（`app.resolve`）。
2. `POST /api/agent/sessions/<chat>/commands` 发送 `start` 命令（幂等键由项目、会话、turn 序号和消息推导；`expectedRevision` 来自 `/state`，409 时重试一次）。
3. 跟随 `GET /api/agent/sessions/<chat>/events?after=<cursor>`（SSE）。持久事件转成进度通知；`run_terminal` / `run_aborted` / `run_failed` 结束这个 turn。流断开后从最后的 sequence 重连；流提前结束则重读 run 记录。
4. 重读项目快照，计算 `version_from→version_to`、delta 和摘要。
5. 持久化事件游标和 run id，宿主超时后 `cassette_status` 可以接回。

run 在服务端是持久的，所以插件没有任务队列也没有 worker：关掉宿主不会丢 run。

## 配置

全部是环境变量，见 [README 的配置表](../README.zh-cn.md#配置)。默认值指向本地 Cassette-Editor 开发栈（API `http://127.0.0.1:8787`，web `http://127.0.0.1:8080`），不带凭证。

工具作用的项目按此优先级：`project_id` 参数 → `state.json` 里当前目录的绑定 → 最近使用的项目。`cassette_project` 的 `action=open` 把已有项目绑定到当前目录。

## 运行本地 Cassette 栈

在 Cassette-Editor 仓库里：

```bash
AGENT_AUTH_ENABLED=false bun run dev:lambda
```

web 编辑器在 `:8080`，API 在 `:8787`，worker 在 `:8788`，插件无需账号即可工作。绕过登录时插件创建匿名 demo 项目（`try-session-<uuid>`），编辑器链接是 `http://127.0.0.1:8080/try?projectSessionId=<uuid>`。

对未改动的后端，视频导入会返回 `video_import_requires_backend_update`：上传注册只接受浏览器端的媒体处理器，需要的改动见 [v2/backend-changes.md](./v2/backend-changes.md)。音频/图片导入、run、历史、导出现在都可用。

## 开发

```bash
uv sync --group dev
uv run pytest -q                                   # 假后端（tests/fake_cassette），约 10 秒
uv run ruff check src tests && uv run ruff format --check src tests
RUN_CASSETTE_LIVE=1 uv run pytest tests/live -q -rs                              # 本地栈
RUN_CASSETTE_LIVE=1 RUN_CASSETTE_LIVE_EXPORT=1 uv run pytest tests/live -q -rs   # 再加一次真实渲染
uv build && uvx --from dist/*.whl oh-my-cassette --version
```

在宿主里直接跑检出的代码：

```bash
claude mcp add cassette-dev -e OH_MY_CASSETTE_LOG=DEBUG -e OH_MY_CASSETTE_HOME=/tmp/omc-dev \
  -- uv run --directory "$PWD" oh-my-cassette
```

`OH_MY_CASSETTE_HOME` 让开发用的状态文件与真实的分开。

### 后端契约

`contracts/` 是从真实后端抓取的 JSON（session state、chat session、snapshot、命令结果、run 事件、媒体状态、导入清单、时间线历史）。`tests/test_models.py` 用它们校验 `cassette/models.py` 里的 pydantic 镜像。响应用 `extra="allow"` 解析，后端新增字段不会弄坏插件；请求用 `extra="forbid"`，请求模型里的拼写错误会在测试里失败而不是打到后端。后端改了 payload 时按 `contracts/README.md` 重新抓取。

### 新增工具

在 `tools/` 实现，在 `server.py` 注册并加入 `TOOL_NAMES`，返回带类型化 `status` 的 dict，用 `@guarded` 包住，补假后端路由和测试，然后写进 `skills/cassette-video-edit/SKILL.md`（再复制到 `.agents/skills/cassette-video-edit/`）。工具没写进 skill 或两份 skill 不一致时 `tests/test_manifests.py` 会失败。

## 排障

| 结果 | 含义 | 处理 |
|---|---|---|
| `error.code = no_project` | 当前目录没有绑定项目，之前也没用过。 | `cassette_project`（`create` 或 `open <id>`）。 |
| `cassette_import` 条目 `file_not_found` / `unsupported_type` | 路径不存在，或扩展名不是视频/音频/图片。 | 用绝对路径；转换文件。 |
| 条目 `video_import_requires_backend_update` | 后端拒绝了 ffmpeg 的准备档案。 | 先落地 [v2/backend-changes.md](./v2/backend-changes.md) 的 P0 改动。 |
| 条目 `failed` 且带 `readiness` | 后端处理失败或超时（`CASSETTE_IMPORT_READY_TIMEOUT_SEC`）。 | 看 worker 日志；重新导入。 |
| `cassette_run` → `needs_input` | agent 提了问题。 | 把 `question` 给用户看，再 `cassette_answer`。 |
| `cassette_run` → `timeout` | turn 超过了 `CASSETTE_RUN_TIMEOUT_SEC` 或宿主的工具超时。 | `cassette_status` 接回；把宿主超时调到一小时。 |
| `cassette_run` → `running` 且带 `note` | 项目上已经有 run 在跑。 | 用 `cassette_status` 等待，或 `cassette_stop`。 |
| `cassette_history` → `rejected`（`at_start`、`at_end`、`target_not_found`） | 没有可撤销/重做的内容，或 group id 未知。 | `cassette_history list`。 |
| `error.code = project_timeline_locked` | run 进行中，历史只读。 | 等 run 结束再试。 |
| 导出 `error.code = timeline_empty` | 活动序列没有 clip。 | 先剪辑。 |
| `error.code = contact_sheet_unavailable` | 没有任何 clip 的原始素材在本机。 | 从本机导入素材，或不传 `contact_sheet`。 |
| `error` 里 HTTP 401/403 | 后端需要账号。 | 设置 `CASSETTE_AUTH_TOKEN` 或邮箱密码，或用 `AGENT_AUTH_ENABLED=false` 启动栈。 |
| Connection refused | `CASSETTE_API_URL` 上没有服务。 | 启动栈；检查宿主配置里的 URL。 |

服务端日志输出到 stderr（`OH_MY_CASSETTE_LOG=DEBUG`），宿主会在 MCP 日志视图里显示。

## 公共仓库安全

不要提交 `.env`、token 或密码、非本地默认值的后端 URL、`tests/fixtures/` 之外的媒体、导出文件，以及 `~/.oh-my-cassette` 里的任何东西。
