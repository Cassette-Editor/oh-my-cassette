# oh-my-cassette v3：独立的 Cassette MCP 服务 + 本地通用 bridge

日期：2026-10-02 · 状态：bridge 已在本仓库实现；MCP 服务在 Cassette-Editor 里开发中 · 契约：[contract.md](./contract.md)

## 1. 为什么要改

2026-09-30 用 v2（0.5.0，未发布）对接 Cassette-Editor `main` a8b4d120f，三处坏了，对应三种耦合：

| 故障 | 插件复制了后端的什么 |
|---|---|
| 模型从 `gpt-5.6-*` 改成 `gpt-6-*`，每次 `cassette_run` 都失败 | 目录和策略（模型列表） |
| 时间线改为 storyline 放置，摘要、contact sheet、导出全部读不到 clip | 业务逻辑（放置计算、导出投影、视频预处理） |
| stop 之后 run 停在 `user_pause`，`cassette_stop` 一直不返回 | 状态机（运行生命周期） |

v2 一共 3765 行。其中约 850 行是在 Python 里重写后端的业务逻辑，约 1200 行是在逐字段复刻后端的接口格式，真正稳定的只有约 800 行传输和 MCP 外壳。后端改得很频繁，插件只要复制了后端的知识，就会被后端的改动弄坏。v2 的代码已经从本仓库删除。

## 2. 决定

```
宿主 ─stdio→ bridge（本仓库）─Streamable HTTP MCP→ Cassette MCP 服务（Cassette-Editor 里的独立进程）─公开 HTTP API→ 后端
```

工具由 **Cassette MCP 服务** 定义：它是 Cassette-Editor 仓库里的独立进程（本地默认 `http://127.0.0.1:8790/mcp`），只通过后端的公开 HTTP API、带调用者自己的令牌访问后端。工具定义和后端放在同一个仓库，随它一起部署和测试。本仓库只留一个**通用 bridge**，它不认识任何具体工具。

bridge 负责的事：

- 在宿主的 stdio 和服务的 Streamable HTTP 之间转发工具列表、工具调用、进度通知和取消；
- 转发服务的 `instructions`；
- 附带令牌（`CASSETTE_AUTH_TOKEN`）；服务回答 401 时报 `bridge.unauthorized`；
- 按服务的 `prepare` 声明在本机预处理本地文件（ffmpeg），走上传握手，并守住本地策略：路径必须在工作区内、类型必须是允许的 MIME、大小不超过上限；
- 把产物下载到工作区；
- 断线后用退避重连，并在工具列表变化时通知宿主；
- 连不上服务或令牌被拒时，只暴露一个 `cassette_bridge_status`。

bridge 不知道的事：工具名、参数、项目和会话、模型、时间线结构、运行状态、导出格式、转码参数的取值。这些全部由服务负责，改动不需要 bridge 发版。

bridge 和服务之间约定的事，都写在契约里：本地文件参数怎么标注、预处理声明和报告、上传握手、下载怎么声明、工作区上下文、错误码、有界等待。

## 3. 本仓库的结构

```
src/oh_my_cassette/
  __main__.py / cli.py   # oh-my-cassette（stdio bridge）、oh-my-cassette check
  contract.py            # 契约里的常量和解析（含 prepare 词汇表）；bridge 与 check 共用
  bridge/
    settings.py          # 环境变量
    errors.py            # bridge.* 错误
    upstream.py          # 到服务的连接：只用 2026-07-28、工具缓存、重连、列表变化、401 与断线
    prepare.py           # ffmpeg/ffprobe：视频产物、音频/图片转换、预处理报告
    jobs.py              # 后台本地阶段：有界等待、preparing、接上同一任务、进度
    files.py             # 本地文件策略与上传握手
    downloads.py         # 产物下载
    server.py            # 宿主侧 MCP server（低层 Server，同时支持两代协议）
  conformance.py         # 一致性检查
tests/reference_remote.py  # 按契约实现的参考服务，可以运行的规格
```

宿主配置只传 `CASSETTE_MCP_URL` 和 `CASSETTE_AUTH_TOKEN`，工具超时统一 600 秒（`.mcp.json`、`opencode.json` 是 600000 毫秒，Codex 的 `tool_timeout_sec` 是 600）。

SKILL.md 不写任何后端工具名，只讲不随版本变的规则：本地文件、`preparing`、有界等待和 `bridge.*` 错误的处理。`tests/test_manifests.py` 会检查这一点：skill 里出现的工具名只能是 `cassette_bridge_status`，并且覆盖代码里用到的所有 `bridge.*` 错误码。

## 4. 连接

连接由 `Upstream.run` 这一个长期任务持有，断开后按 1→30 秒退避重连。宿主发来请求而当前没连上时，会立刻触发一次重连，并且只等这一次尝试的结果，所以服务恢复后下一次调用就能用上，服务还没恢复时也能很快报 `bridge.backend_unreachable`。

- 单次调用没有总时限。HTTP 读空闲上限 60 秒（服务每 15 秒发一次 SSE 注释保活），超过就当作断线。断线、网关错误（502/503/504）都报 `bridge.backend_unreachable`（可重试），并说明服务的工作可能还在运行、再次调用即可接上。
- MCP SDK 把 HTTP 401 报成普通的内部错误，bridge 用 httpx 的响应钩子数 401 来区分：连接时出现 401，bridge 进入 unauthorized 状态，工具列表只剩 `cassette_bridge_status`；调用中出现 401（令牌被吊销），这次调用报 `bridge.unauthorized`，连接标记为坏，下次重连。
- 单个 SSE 事件上限 32 MiB。

## 5. 本地阶段

一次带本地文件的调用分两段：本地阶段（预处理 + 上传）和转发。

- **预处理**（`prepare.py`）：视频用一次 ffmpeg 产出全部产物，`split` 保证各产物帧数相同；恒定帧率、固定 GOP（`-sc_threshold 0`）、等比缩进框内且不放大、偶数尺寸、应用旋转、HDR 有 zscale 时色调映射、统一标为 BT.709、源有音频才输出 AAC、`+faststart`；进度来自 `-progress pipe:1`。之后用 ffprobe 读源文件和每个产物，生成预处理报告。服务不直接接受的音频转 FLAC，图片转 JPEG。中间文件放在 bridge 的临时目录下，用完删除。
- **后台任务**（`jobs.py`）：本地阶段在 bridge 自己的任务组里运行，键是（工具名、`prepare` 摘要、把路径换成 `clientRef` 后的参数、每个文件的真实路径 + `mtime_ns` + 大小）。参数在键里，因为 ref 可能限定在某个项目里。一次调用最多等 `CASSETTE_LOCAL_WAIT_SEC`（默认 240 秒），没完成就回答非错误的 `{"status": "preparing", "progress": …}`；再次调用接上同一个任务。完成的 ref 在进程生命周期内缓存；失败只报告一次，然后丢弃。
- **进度**：等待期间向宿主发进度通知，最多每秒一条，没有变化时也至少每 15 秒一条。进度值单调递增。
- **`outputSchema`**：`preparing` 不符合工具的 `outputSchema`，所以 bridge 列出声明了 `localFiles` 的工具时去掉 `outputSchema`。
- 转发时 `_meta` 里带 `elapsedSeconds`，服务从自己的等待窗口里扣掉它。

## 6. 服务的工具面（非规范）

Cassette-Editor 的 MCP 服务目前提供项目（打开/新建）、导入（`localFiles` + `prepare`，返回每个文件的 published / processing / failed）、对话（`cassette_turn`：发消息或用 `run_id` 续等，返回 running / completed / needs_input / paused / failed / stopped / busy 等）、回答、停止、历史与撤销（undo / redo / revert_run）、导出（running / completed，结果用 `downloads` 声明）。这些名字和形状由服务决定，bridge 和 skill 都不依赖它们；本仓库只有 live 测试按服务当前的工具面写。

v2 的工作流规则（原话转发、按类型化 `status` 路由、只在用户要求时导出、所有说法都来自工具结果）属于服务，写在服务的 `instructions` 里。

## 7. 测试

- **bridge 单元和集成测试**：参考服务跑在真实的 uvicorn HTTP 上（要求 bearer token），和 MCP 服务一样只提供 2026-07-28 协议；另有一个只会握手时代协议的变体，用来证明 bridge 和一致性检查会拒绝它。宿主一侧用进程内客户端，覆盖 2026-07-28 和握手两代协议。本地阶段用假的预处理器测试等待、`preparing`、接上任务和失败。
- **预处理**：`test_prepare.py` 用真实 ffmpeg 处理生成的小样本（HEVC mkv、10 位 PQ HDR、旋转 90°、641×359、无音频、VFR、时间码、PNG/TIFF/HEIC、WAV/AIFF），用 ffprobe 检查每个产物和报告的形状。
- **stdio 端到端**：像宿主那样启动 `python -m oh_my_cassette`。
- **一致性检查**：对参考服务必须全部通过，对故意写坏的变体（坏的 pointer、坏的 `prepare`、缺上传工具、begin 不给 PUT 目标、缺令牌、只会握手时代协议）必须报错。服务在自己的 CI 里对本地栈运行同一套检查。
- **live**：`tests/live/test_live_backend.py` 对真实的本地栈跑一遍打开项目、导入、对话、导出、撤销，默认跳过。

## 8. 待定

- `_meta` 命名空间 `io.github.cassette-editor/` 取自 MCP 注册名。如果有产品域名，可以改成它的反向写法，改动只在 `contract.py` 一处。
- 默认服务地址目前是本地的 `http://127.0.0.1:8790/mcp`，发布前要换成生产地址。
- OAuth（MCP 授权规范）、分片上传、resources/prompts 转发都留到后续版本。
- `prepare` 的 v1 取值只覆盖 H.264/AAC/MP4、FLAC、JPEG 和 8 位；服务要求别的取值时 bridge 报 `bridge.contract_violation`，需要 bridge 发版。
- 各宿主是否处理 `tools/list_changed` 还没逐一验证。不处理的宿主要重启才能看到新工具，`cassette_bridge_status` 会提示这一点。
