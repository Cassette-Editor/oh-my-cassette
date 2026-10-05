<p align="center">
  <img src="./assets/logo-400.png" alt="Oh My Cassette" width="120" />
</p>

<h1 align="center">Oh My Cassette</h1>

<p align="center">
  在 Claude Code、Codex、OpenCode、Hermes 里，通过 <a href="https://trycassette.online">Cassette</a> 的剪辑 agent 剪视频。<br />
  <sub>一个发布在 PyPI 上的小型 MCP bridge。剪辑工具由 Cassette MCP 服务提供，服务更新后不用升级插件也能用上。</sub>
</p>

<p align="center">
  <a href="https://pypi.org/project/oh-my-cassette/"><img alt="PyPI" src="https://img.shields.io/pypi/v/oh-my-cassette?label=oh-my-cassette" /></a>
  <a href="./LICENSE"><img alt="MIT" src="https://img.shields.io/badge/license-MIT-blue" /></a>
  <a href="./README.md">English</a>
</p>

---

## 30 秒安装

需要 [uv](https://docs.astral.sh/uv/)（首次启动时自动拉取 Python 和包）。

```bash
# Claude Code
claude plugin marketplace add Cassette-Editor/oh-my-cassette
claude plugin install oh-my-cassette@cassette-editor
```

```bash
# Codex
codex plugin marketplace add https://github.com/Cassette-Editor/oh-my-cassette.git
codex plugin add oh-my-cassette@cassette-editor
```

```bash
# Hermes
hermes mcp add cassette --command uvx --args oh-my-cassette==0.5.0
```

OpenCode 与其它 MCP 宿主见下文[安装](#安装)。运行 `uvx oh-my-cassette==0.5.0 login`，在浏览器中用邮箱验证码登录并授权。如果宿主不支持刷新工具列表，重启 agent 后直接说：

> *把 ./footage 里的 mp4 导入，剪一个 30 秒的旅行 vlog，开头加标题。*

## 概览

Oh My Cassette 把你的编码 agent 接到 Cassette 的剪辑 agent 上。它是一个本地 stdio MCP server，
转发到 Cassette MCP 服务（`CASSETTE_MCP_URL`），再由该服务调用 Cassette 后端：

```text
Claude Code / Codex / OpenCode / Hermes  ──stdio──▶  oh-my-cassette  ──HTTP──▶  Cassette MCP 服务  ──▶  Cassette 后端
```

- **工具属于服务。** 服务列出什么（项目、剪辑对话、回答提问、撤销、导出），你的 agent 就看到什么，
  描述和工作流说明也都来自服务。Cassette 改了，工具跟着变，插件不用发版。
- **bridge 只做必须在你电脑上做的事。** 调用里写到的本地文件先预处理再上传（只限工作区内、只限媒体类型）：
  视频用 ffmpeg 转码成服务声明的各个产物，服务不直接接受的音频和图片先转换。导出的文件下载到
  `cassette-exports/`。每次调用附带工作区标识，服务可以据此记住一个目录对应的项目。
- **长任务不会让宿主超时。** 每次调用只等一个有界窗口；本地预处理没做完时回答 `preparing`，工作在后台继续，
  用同样的参数再调用一次就能接上。
- **重新部署也不怕。** bridge 会自动重连，自己的错误用 `bridge.*` 代码并带 `retryable` 标记；
  服务连不上或不接受令牌时，工具列表里只有一个 `cassette_bridge_status`，调用它能看到原因。

插件和服务之间的约定写在 [docs/v3/contract.md](./docs/v3/contract.md)，`oh-my-cassette check`
可以按它检查一个服务。

### 案例视频

六个真实案例，每个都附完整提示词、输入素材和处理时长：[docs/showcase.zh-cn.md](./docs/showcase.zh-cn.md)。

## 环境要求

- 实现[约定 v1](./docs/v3/contract.md) 的 Cassette MCP 服务，以及获准使用 Cassette 的账号。可以用
  `uvx oh-my-cassette==0.5.0 check --url <端点>` 检查。服务连不上或不接受令牌时，
  工具列表里只有 `cassette_bridge_status`。
- [uv](https://docs.astral.sh/uv/getting-started/installation/) 0.5+。`uvx oh-my-cassette==0.5.0` 会自行解析 Python 3.11–3.13。
- 导入视频需要 [ffmpeg](https://ffmpeg.org/download.html)（含 ffprobe）：`brew install ffmpeg`、
  `sudo apt install ffmpeg` 或 `winget install ffmpeg`。视频在你的电脑上转码后再上传；带 `zscale` 的
  ffmpeg 还能正确地把 HDR 素材色调映射到 SDR。

每个工具接受哪些格式、上传前怎样预处理，都由服务声明（最多是视频、音频和图片）。

## 配置

连接 Web 时先运行 `oh-my-cassette login --target web`。连接已打开的 Desktop 本地 profile 时，显式使用 `--target desktop`，并在本地授权页允许连接。Desktop 无需云端登录，离线也能配对；失败不会切换到 Web。凭证保存在系统钥匙串，无需复制 token。详见[登录说明](./docs/oauth.md)。下面是可选覆盖配置。

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `CASSETTE_MCP_URL` | 已保存目标，否则 Cassette Web | 显式覆盖 MCP 端点。 |
| `CASSETTE_AUTH_TOKEN` | 未设置 | 高级兼容入口；默认使用 `login` 和系统钥匙串。仍受账号、scope 和 resource 校验。不要写入共享配置。 |
| `CASSETTE_WORKSPACE` | 宿主启动 server 时的目录 | 相对路径从这里解析，服务也可以据此记住这个目录对应的项目。 |
| `CASSETTE_ALLOWED_ROOTS` | 未设置 | 额外允许读取本地文件的目录（用 `:` 分隔，Windows 上用 `;`）。 |
| `CASSETTE_DOWNLOAD_DIR` | `<工作区>/cassette-exports` | 导出文件保存的位置。 |
| `CASSETTE_MAX_UPLOAD_MB` / `CASSETTE_MAX_DOWNLOAD_MB` | `4096` / `16384` | 单个文件的大小上限。 |
| `CASSETTE_UPLOAD_ANY_TYPE` | 关闭 | 工具接受时，允许上传非媒体文件。 |
| `CASSETTE_LOCAL_WAIT_SEC` | `240` | 一次调用最多等本地预处理和上传多久，超过就回答 `preparing`。 |
| `CASSETTE_FFMPEG` / `CASSETTE_FFPROBE` | 从 `PATH` 查找 | 预处理本地媒体用的 ffmpeg 和 ffprobe。 |
| `CASSETTE_TEMP_DIR` | `<系统临时目录>/oh-my-cassette` | 预处理产物在上传前存放的位置。 |
| `CASSETTE_CONNECT_TIMEOUT_SEC` | `10` | 连接服务时的最长等待时间。 |
| `OH_MY_CASSETTE_LOG` | `WARNING` | stderr 日志级别。 |

## 安装

### Claude Code

推荐用顶部两条插件命令。先运行 `oh-my-cassette login` 完成浏览器登录，插件沿用已保存的目标，并以 10 分钟工具超时启动 `uvx oh-my-cassette==0.5.0`。

不用插件、只在项目范围接入：把 [`.mcp.json`](./.mcp.json) 复制到你的项目并替换 `${user_config.*}`，或者：

```bash
claude mcp add --transport stdio cassette -- uvx oh-my-cassette==0.5.0
```

skill 在 [`skills/cassette-video-edit/SKILL.md`](./skills/cassette-video-edit/SKILL.md)，随插件一起安装。

### Codex

顶部两条插件命令（[`.codex-plugin/plugin.json`](./.codex-plugin/plugin.json) 内联声明了 server，`tool_timeout_sec: 600`，并从你的 shell 透传 `CASSETTE_*` 变量）。或者直接加 server：

```bash
codex mcp add cassette -- uvx oh-my-cassette==0.5.0
```

### OpenCode

在 `opencode.json`（项目或 `~/.config/opencode/opencode.json`）里加：

```json
{
  "mcp": {
    "cassette": {
      "type": "local",
      "command": ["uvx", "oh-my-cassette==0.5.0"],
      "timeout": 600000
    }
  }
}
```

再把 skill 复制到 OpenCode 的 skills 目录：

```bash
mkdir -p ~/.config/opencode/skills/cassette-video-edit
curl -fsSL https://raw.githubusercontent.com/Cassette-Editor/oh-my-cassette/main/skills/cassette-video-edit/SKILL.md \
  -o ~/.config/opencode/skills/cassette-video-edit/SKILL.md
```

OpenCode 没有 MCP elicitation，没关系：剪辑 agent 的提问会作为普通的工具结果返回，怎么回答由服务的工具说明。

### Hermes

```bash
hermes mcp add cassette --command uvx --args oh-my-cassette==0.5.0 \

mkdir -p ~/.hermes/skills/cassette-video-edit
curl -fsSL https://raw.githubusercontent.com/Cassette-Editor/oh-my-cassette/main/skills/cassette-video-edit/SKILL.md \
  -o ~/.hermes/skills/cassette-video-edit/SKILL.md
```

在 `~/.hermes/config.yaml` 里把 `mcp_servers.cassette.timeout` 设为 `600`，一次调用里的有界等待才放得下。Hermes 在这里只是普通 MCP 宿主：0.4 的 gateway/插件层已经删除。

### 其它 MCP 宿主

命令 `uvx`，参数 `["oh-my-cassette==0.5.0"]`，stdio 传输，工具超时 10 分钟，再加上面的环境变量。没有 uv 的机器可以用 `pipx run oh-my-cassette==0.5.0`。

## 一个 turn 长什么样

```text
你：    导入 intro.mp4 和 beach.mov，剪一个 20 秒的片子，标题 "Kota Kinabalu"。
agent： （调用服务的导入工具，参数 ["intro.mp4", "beach.mov"]）
        bridge：转码两个视频并上传，把服务给的素材引用传过去
        （调用服务的剪辑工具，参数是你的原话；运行期间持续推送进度）
        "完成。用两段素材剪了 20 秒，开头是标题（v0→v3）。打开看：<编辑器链接>"
你：    导出。
agent： （调用服务的导出工具）
        bridge：把 MP4 下载到 cassette-exports/kota-kinabalu.mp4
```

编辑器链接由服务给出：在浏览器里打开，可以实时看到 run 的过程，也能随时手动接管。

## 检查服务

```bash
uvx oh-my-cassette==0.5.0 check --url http://127.0.0.1:8790/mcp --upload \
  --arguments '{"project_id": "<一个测试项目>"}'
```

每项检查输出一行（传输与令牌、约定声明、instructions、工具 schema、本地文件和 `prepare` 声明；加 `--upload`
时还会真的走一次上传，`--arguments` 是文件工具的其他参数），有失败时退出码为 1。`--json` 输出机器可读结果，
方便放进服务的 CI。服务必须支持 MCP 2026-07-28；只会握手时代 `initialize` 的服务在传输检查上判失败，
bridge 也会以 `bridge.protocol_unsupported` 拒绝它。

## 更新

改宿主配置里的版本号（`oh-my-cassette==<新版本>`）或重装插件；`uvx` 下次启动时拉取新 wheel。版本记录见 [CHANGELOG.md](./CHANGELOG.md)。

## 开发

```bash
git clone https://github.com/Cassette-Editor/oh-my-cassette && cd oh-my-cassette
uv sync --group dev
uv run pytest -q                                   # 参考服务跑在本机回环地址上，不联网；需要 ffmpeg
uv run oh-my-cassette                              # bridge 本体（stdio）
uv run oh-my-cassette check --url <endpoint>       # 一致性检查
```

bridge 的设计见 [docs/v3/design.md](./docs/v3/design.md)；`tests/reference_remote.py` 是一个可执行的
参考服务。本地栈和 live 测试见 [docs/development.zh-cn.md](./docs/development.zh-cn.md)，PyPI 发布流程见
[RELEASING.md](./RELEASING.md)。

## 许可证

MIT。Oh My Cassette 是客户端；Cassette 服务有自己的条款。

<sub>MCP registry: `mcp-name: io.github.Cassette-Editor/oh-my-cassette`</sub>
