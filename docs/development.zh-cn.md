# 开发与排障

[← 返回 README](../README.zh-cn.md)

> [!TIP]
> 加入我们的 Discord 社区和 `oh-my-cassette` 用户一起交流
>
> [![Discord](https://img.shields.io/discord/1514649803626250452?style=for-the-badge&logo=discord&logoColor=white&label=Discord&labelColor=black&color=5865F2)](https://discord.gg/qd9NY4k8d7)

## 整体结构

```text
宿主（Claude Code / Codex / OpenCode / Hermes）
  └─ stdio ─ uvx oh-my-cassette              src/oh_my_cassette/cli.py
               bridge/server.py                工具、instructions、调用与进度、cassette_bridge_status
               bridge/upstream.py              上游的一个 MCP 客户端会话；重连；401 / 连不上
               bridge/files.py                 本地文件策略与上传握手
               bridge/prepare.py               ffmpeg：视频产物、音频/图片转换、预处理报告
               bridge/jobs.py                  后台的本地阶段、`preparing`、接上同一任务
               bridge/downloads.py             声明的下载，存到 cassette-exports/
               contract.py                     契约里的标注和 `prepare` 词汇表
               conformance.py                  `oh-my-cassette check`
                        │ Streamable HTTP（MCP 2026-07-28）+ bearer token
                        ▼
              Cassette MCP 服务                 在 Cassette-Editor 里（`mcp/`），默认 http://127.0.0.1:8790/mcp
                        │ 公开 HTTP API，带调用者自己的令牌
                        ▼
              Cassette-Editor 后端
```

bridge 从不写死后端工具的名字。哪些参数是本地路径、怎样预处理、哪些结果带下载，全部来自
[v3/contract.md](./v3/contract.md) 规定的 `_meta` 标注；设计见 [v3/design.md](./v3/design.md)。

一次带本地文件的调用：

1. bridge 按本地策略检查每个路径（工作区或 `CASSETTE_ALLOWED_ROOTS`、媒体类型、大小），并用工具名、
   `prepare` 摘要、参数和文件身份给这次调用定一个键。
2. 后台任务预处理文件（ffmpeg）并走上传握手。这次调用最多等 `CASSETTE_LOCAL_WAIT_SEC`；任务还没完成时回答
   `{"status": "preparing"}`，下一次同样的调用会接上同一个任务。
3. 拿到 ref 后，bridge 转发原调用，`_meta` 里带 `elapsedSeconds`。服务同样只等一个有界窗口，工作超出窗口时回答
   `running` / `processing`。

## 运行本地栈

在 Cassette-Editor 仓库里：

```bash
bun run dev:lambda      # web 编辑器、:8787 上的 API、worker
bun run dev:mcp         # 127.0.0.1:8790 上的 MCP 服务
```

使用 `CASSETTE_MCP_URL=http://127.0.0.1:8790/mcp uv run oh-my-cassette login --target web` 登录本地服务。服务必须提供 OAuth resource metadata，凭证保存在系统钥匙串中，详见 [OAuth](oauth.md)。

## 开发

```bash
uv sync --group dev
uv run pytest -q -rs                               # 参考服务跑在回环地址上；test_prepare 需要 ffmpeg
uv run ruff check . && uv run ruff format --check .
uv run oh-my-cassette check --url http://127.0.0.1:8790/mcp --upload
RUN_CASSETTE_LIVE=1 uv run pytest tests/live -q -rs          # 本地栈，包括一次真实导出
uv build && uvx --from dist/*.whl oh-my-cassette --version
```

`tests/reference_remote.py` 是契约的可执行参考服务：要求 bearer token、声明 `prepare`、按内容去重、检查每次 PUT。
`tests/test_prepare.py` 用真实 ffmpeg 处理生成的小样本（HEVC、10 位 PQ、旋转、奇数尺寸、VFR、时间码、图片、音频），
并用 ffprobe 检查每个产物；没有 ffmpeg 时整个模块跳过。live 测试需要带 libx265 的 ffmpeg，导出走后端配置的渲染服务，
可能产生费用。

在宿主里运行当前仓库：

```bash
claude mcp add cassette-dev -e OH_MY_CASSETTE_LOG=DEBUG \
  -- uv run --directory "$PWD" oh-my-cassette
```

skill 有两份（`skills/cassette-video-edit/SKILL.md` 和 `.agents/skills/cassette-video-edit/SKILL.md`）；两份不一致、
skill 写了后端工具名、或漏了某个 `bridge.*` 错误码时，`tests/test_manifests.py` 会失败。

## 排障

| 结果 | 含义 | 怎么办 |
|---|---|---|
| 只列出 `cassette_bridge_status` | 服务连不上、不接受令牌，或要求更新的 bridge。 | 调用它：`error_code` 和 `error` 会说明是哪种。 |
| `bridge.unauthorized` | 服务回答 HTTP 401。 | 运行 `oh-my-cassette login`，并移除过期的手动 token 覆盖。 |
| 调用中出现 `bridge.backend_unreachable` | 连接断开，或 60 秒没有任何数据。 | 用同样的参数再调用一次：服务的工作可能还在运行。 |
| `{"status": "preparing"}` | 本地预处理或上传还没完成。 | 用同样的参数再调用一次，继续等待。 |
| `bridge.ffmpeg_unavailable` | PATH 上没有 ffmpeg / ffprobe。 | 安装 ffmpeg，或设置 `CASSETTE_FFMPEG` 和 `CASSETTE_FFPROBE`。 |
| `bridge.prepare_failed` | ffmpeg 读不了或转换不了某个文件；消息末尾是它的 stderr。 | 检查文件；必要时手动转换。 |
| `bridge.contract_violation` | 服务的标注不合法（例如 `prepare`）。 | 对它运行 `oh-my-cassette check`。 |
| Connection refused | `CASSETTE_MCP_URL` 上没有服务。 | 启动 MCP 服务；检查宿主配置里的 URL。 |

bridge 的日志输出到 stderr（`OH_MY_CASSETTE_LOG=DEBUG`），宿主会在 MCP 日志视图里显示。

## 公共仓库安全

不要提交 `.env`、token 或密码、非本地默认值的服务 URL、`tests/fixtures/` 之外的媒体，以及导出文件。
