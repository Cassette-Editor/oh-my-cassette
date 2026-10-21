<p align="center">
  <img src="https://raw.githubusercontent.com/Cassette-Editor/oh-my-cassette/main/assets/banner.jpg" width="80%" alt="Oh My Cassette" />
</p>

<h1 align="center">Oh My Cassette</h1>

<p align="center">
  在 Claude Code、Codex、OpenCode、Hermes 里，和 <a href="https://trycassette.online">Cassette</a> 的剪辑 agent 对话来剪视频。<br />
  <sub>一个发布在 PyPI 上的小型 MCP bridge。剪辑工具由 Cassette MCP 服务提供，服务更新后不用升级插件也能用上。</sub>
</p>

<p align="center">
  <a href="https://pypi.org/project/oh-my-cassette/"><img alt="PyPI" src="https://img.shields.io/pypi/v/oh-my-cassette?label=oh-my-cassette" /></a>
  <a href="https://github.com/Cassette-Editor/oh-my-cassette/blob/main/LICENSE"><img alt="MIT" src="https://img.shields.io/badge/license-MIT-blue" /></a>
  <a href="https://discord.gg/qd9NY4k8d7"><img alt="Discord" src="https://img.shields.io/discord/1514649803626250452?logo=discord&logoColor=white&label=Discord&color=5865F2" /></a>
  <a href="https://github.com/Cassette-Editor/oh-my-cassette/blob/main/README.md">English</a>
</p>

---

<p align="center">
  <img src="https://raw.githubusercontent.com/Cassette-Editor/oh-my-cassette/main/assets/demo-claude-code.gif" width="100%" alt="一次 Claude Code 会话：导入五段 4K 影片和一首曲子，要一支 30 秒预告片，改一次，导出" />
</p>

**一次 Claude Code 会话，画面里的字就是实际输入。** 五段 4K 旅行影片（共 10 分半）和一首曲子导入项目，四句话的需求和一句修改原样交给
Cassette agent，一支 30 秒 1080p 的 MP4 落到 `cassette-exports/`。等待部分做了快进：实际导入用了 4 分半，第一版 9 分半，修改 3 分半，
导出 1 分钟。

> *Cut a 30-second teaser for Turkey from these five films, on the beat of Pamgaea.mp3. Open on a hot-air balloon
> lighting up at dawn in Cappadocia, travel from the ancient ruins to the sea, name each place when it first appears,
> and end on the misty lake with the title TURKEY.*
>
> *The ruins part drags. Keep only the two best Ephesus shots, and put the camel right after the balloons.*

<p align="center">
  <a href="https://github.com/Cassette-Editor/oh-my-cassette/releases/download/v0.5.0/turkey-teaser.mp4"><img src="https://raw.githubusercontent.com/Cassette-Editor/oh-my-cassette/main/assets/turkey-teaser.jpg" width="100%" alt="成片截图：黎明的热气球、骆驼、以弗所两个镜头、达特恰海岸、雾中湖面与 TURKEY 标题" /></a>
  <br /><sub><a href="https://github.com/Cassette-Editor/oh-my-cassette/releases/download/v0.5.0/turkey-teaser.mp4">观看这支预告片（MP4，30 秒，有声）</a>。素材与音乐署名见<a href="#署名">下文</a>。</sub>
</p>

## 30 秒安装

需要 [uv](https://docs.astral.sh/uv/)（首次启动时自动拉取 Python 和包），导入视频还需要 [ffmpeg](https://ffmpeg.org/download.html)。

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

> *新建一个 Cassette 项目，导入 ./footage 里的素材，剪一个 30 秒的旅行短片，开头加标题。*

## 从 0.4 升级

0.5 是重写。0.4 插件自带剪辑运行时、配乐搜索和 Hermes 网关；0.5 只是通往 Cassette MCP 服务的一层薄 bridge，你的 agent 看到的工具都来自服务。

- **重新登录**：`uvx oh-my-cassette==0.5.0 login`。0.4 的密码和令牌不再使用。
- **工具是新的。** 项目、剪辑对话、提问、计划、撤销、素材、分析和导出都由服务提供并自带说明；0.4 的工具名（`cassette_run_job`、
  `cassette_ingest_media`、`cassette_match_bgm` 等）已删除。重新安装插件，或把宿主配置改为 `uvx oh-my-cassette==0.5.0`。
- **项目在你的 Cassette 账号里**，和网页版、Cassette for Mac 里的是同一批；每次回答带的编辑器链接打开的就是当前项目。0.4 的会话不会迁移。
- **Hermes** 现在是普通 MCP 宿主，请移除 0.4 的网关插件。

## 项目保留期

bridge 新建的是云端项目，闲置三天后 Cassette 会连同素材一起删除；每次使用都会顺延，每次回答都写明删除时间（`retention_expires_at`）。
已导出的文件在你电脑的 `cassette-exports/` 里，不受影响。在 Cassette for Mac 里建的项目会一直保留，bridge 可以在那台 Mac 在线时打开、
编辑和导出它，但它的素材要在 App 里导入。

## 概览

Oh My Cassette 把你的编码 agent 接到 Cassette 的剪辑 agent 上。它是一个本地 stdio MCP server，
转发到 Cassette MCP 服务（`CASSETTE_MCP_URL`），再由该服务调用 Cassette 后端：

```text
Claude Code / Codex / OpenCode / Hermes  ──stdio──▶  oh-my-cassette  ──HTTP──▶  Cassette MCP 服务  ──▶  Cassette 后端
```

- **工具属于服务。** 服务列出什么（项目、剪辑对话、回答提问、撤销、导出），你的 agent 就看到什么，
  描述和工作流说明也都来自服务。Cassette 改了，工具跟着变，插件不用发版。
- **bridge 只做必须在你电脑上做的事。** 调用里写到的本地文件先预处理再上传（只限工作区内、只限媒体类型）：
  视频用 ffmpeg 转码成服务声明的各个产物，服务不直接接受的音频和图片先转换。每个文件准备好就开始上传，
  同时准备下一个，分片上传、最多四路并发；上传中断的文件再次导入时会接上之前那次导入。导出的文件下载到
  `cassette-exports/`。每次调用附带工作区标识，服务可以据此记住一个目录对应的项目。
- **长任务不会让宿主超时。** 每次调用只等一个有界窗口；本地预处理没做完时回答 `preparing`，工作在后台继续，
  用同样的参数再调用一次就能接上。
- **重新部署也不怕。** bridge 会自动重连；带请求标识的调用在连接断开或被服务拒绝时由 bridge 自己重发；
  中断的下载会续传；自己的错误用 `bridge.*` 代码并带 `retryable` 标记（服务给出等待时间时还带
  `retry_after_seconds`）；服务连不上或不接受令牌时，工具列表里只有一个 `cassette_bridge_status`，
  调用它能看到原因。

插件和服务之间的约定定义在 [`src/oh_my_cassette/contract.py`](https://github.com/Cassette-Editor/oh-my-cassette/blob/main/src/oh_my_cassette/contract.py)，
`oh-my-cassette check` 可以按它检查一个服务。

## 环境要求

- 实现约定 v1 的 Cassette MCP 服务，以及获准使用 Cassette 的账号。可以用
  `uvx oh-my-cassette==0.5.0 check --url <端点>` 检查。服务连不上或不接受令牌时，
  工具列表里只有 `cassette_bridge_status`。
- [uv](https://docs.astral.sh/uv/getting-started/installation/) 0.5+。`uvx oh-my-cassette==0.5.0` 会自行解析 Python 3.11 或更新版本。
- 导入视频需要 [ffmpeg](https://ffmpeg.org/download.html)（含 ffprobe）：`brew install ffmpeg`、
  `sudo apt install ffmpeg` 或 `winget install ffmpeg`。视频在你的电脑上转码后再上传；带 `zscale` 的
  ffmpeg 还能正确地把 HDR 素材色调映射到 SDR。

每个工具接受哪些格式、上传前怎样预处理，都由服务声明（最多是视频、音频和图片）。

## 配置

先运行一次 `oh-my-cassette login`，用你项目所在的 Cassette 账号登录：浏览器里输入邮件验证码并确认授权。一次登录即可访问该账号的所有项目，无论是在网页版还是 Cassette for Mac 里创建的。素材在 Mac 上的项目在那台 Mac 的 App 里打开；读取它的画面或声音、导出它都需要那台 Mac 在线，它的素材也在那台 Mac 上导入。凭证保存在系统钥匙串，无需复制 token。下面是可选覆盖配置。

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `CASSETTE_MCP_URL` | 已保存的连接，否则 `https://mcp.trycassette.online/mcp` | 显式覆盖 MCP 端点。 |
| `CASSETTE_AUTH_TOKEN` | 未设置 | 高级兼容入口；默认使用 `login` 和系统钥匙串。仍受账号、scope 和 resource 校验。不要写入共享配置。 |
| `CASSETTE_WORKSPACE` | 宿主启动 server 时的目录 | 相对路径从这里解析，本地文件只能来自这里，服务也可以据此记住这个目录对应的项目。宿主在整个磁盘根目录或主目录启动 server 时，不会读取任何文件，直到设置这一项或 `CASSETTE_ALLOWED_ROOTS`。 |
| `CASSETTE_ALLOWED_ROOTS` | 未设置 | 额外允许读取本地文件的目录（用 `:` 分隔，Windows 上用 `;`）。 |
| `CASSETTE_DOWNLOAD_DIR` | `<工作区>/cassette-exports` | 导出文件保存的位置。默认位置必须在工作区内：指向别处的 `cassette-exports` 符号链接会被拒绝。 |
| `CASSETTE_MAX_UPLOAD_MB` / `CASSETTE_MAX_DOWNLOAD_MB` | `4096` / `16384` | 单个文件的大小上限。 |
| `CASSETTE_UPLOAD_ANY_TYPE` | 关闭 | 工具接受时，允许上传非媒体文件。 |
| `CASSETTE_LOCAL_WAIT_SEC` | `240` | 一次调用最多等本地预处理和上传多久，超过就回答 `preparing`。 |
| `CASSETTE_FFMPEG` / `CASSETTE_FFPROBE` | 从 `PATH` 查找 | 预处理本地媒体用的 ffmpeg 和 ffprobe。 |
| `CASSETTE_TEMP_DIR` | 系统临时目录 | 每次上传在这里新建自己的私有目录（`oh-my-cassette-job-*`），存放上传前的预处理产物。 |
| `CASSETTE_CONNECT_TIMEOUT_SEC` | `10` | 连接服务时的最长等待时间。 |
| `CASSETTE_MCP_HOST` | 未设置 | 宿主没有报出自己的名字时，发给服务的宿主名。 |
| `OH_MY_CASSETTE_LOG` | `WARNING` | stderr 日志级别。 |

## 安装

### Claude Code

推荐用顶部两条插件命令。先运行 `oh-my-cassette login` 完成浏览器登录，插件沿用已保存的目标，并以 10 分钟工具超时启动 `uvx oh-my-cassette==0.5.0`。

不用插件、只在项目范围接入：把 [`.mcp.json`](https://github.com/Cassette-Editor/oh-my-cassette/blob/main/.mcp.json) 复制到你的项目，或者：

```bash
claude mcp add --transport stdio cassette -- uvx oh-my-cassette==0.5.0
```

skill 在 [`skills/cassette-video-edit/SKILL.md`](https://github.com/Cassette-Editor/oh-my-cassette/blob/main/skills/cassette-video-edit/SKILL.md)，随插件一起安装。

### Codex

顶部两条插件命令（[`.codex-plugin/plugin.json`](https://github.com/Cassette-Editor/oh-my-cassette/blob/main/.codex-plugin/plugin.json) 内联声明了 server，`tool_timeout_sec: 600`，并从你的 shell 透传 `CASSETTE_*` 变量）。或者直接加 server：

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
curl -fsSL https://raw.githubusercontent.com/Cassette-Editor/oh-my-cassette/v0.5.0/skills/cassette-video-edit/SKILL.md \
  -o ~/.config/opencode/skills/cassette-video-edit/SKILL.md
```

OpenCode 没有 MCP elicitation，没关系：剪辑 agent 的提问会作为普通的工具结果返回，怎么回答由服务的工具说明。

### Hermes

```bash
hermes mcp add cassette --command uvx --args oh-my-cassette==0.5.0

mkdir -p ~/.hermes/skills/cassette-video-edit
curl -fsSL https://raw.githubusercontent.com/Cassette-Editor/oh-my-cassette/v0.5.0/skills/cassette-video-edit/SKILL.md \
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

改宿主配置里的版本号（`oh-my-cassette==<新版本>`）或重装插件；`uvx` 下次启动时拉取新 wheel。版本记录见 [CHANGELOG.md](https://github.com/Cassette-Editor/oh-my-cassette/blob/main/CHANGELOG.md)。

## 开发

```bash
git clone https://github.com/Cassette-Editor/oh-my-cassette && cd oh-my-cassette
uv sync --group dev
uv run pytest -q                                   # 参考服务跑在本机回环地址上，不联网；需要 ffmpeg
uv run oh-my-cassette                              # bridge 本体（stdio）
uv run oh-my-cassette check --url <endpoint>       # 一致性检查
```

`tests/reference_remote.py` 是一个可执行的参考服务。PyPI 发布流程见 [RELEASING.md](https://github.com/Cassette-Editor/oh-my-cassette/blob/main/RELEASING.md)。

## 署名

演示素材作者 [yasin ilcebay](https://vimeo.com/yasinilcebay)，采用 [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/) 许可，
来自 Wikimedia Commons：
[Aizanoi Temple of Zeus](https://commons.wikimedia.org/wiki/File:Aizonai_Temple_of_Zeus_(4K)_-_TURKEY.webm)、
[Cappadocia](https://commons.wikimedia.org/wiki/File:Cappadocia_4K_-_TURKEY.webm)、
[Datça](https://commons.wikimedia.org/wiki/File:Dat%C3%A7a_-_TURKEY_4K.webm)、
[Ephesus](https://commons.wikimedia.org/wiki/File:Ephesus_Ancient_City_-_Celsus_Library_4K.webm)、
[Gölcük Lake](https://commons.wikimedia.org/wiki/File:G%C3%B6lc%C3%BCk_Natural_Park_4K_-_Turkey.webm)。
预告片对素材做了剪裁和重排，并加上了地名、标题和音乐。

音乐为 "Pamgaea" Kevin MacLeod (incompetech.com)<br />
Licensed under Creative Commons: By Attribution 4.0<br />
https://creativecommons.org/licenses/by/4.0/<br />
预告片使用了其中的一段。

## 许可证

代码采用 MIT 许可。Oh My Cassette 是客户端；Cassette 服务有自己的条款。

演示录屏（`assets/demo-claude-code.gif`）、预告片截帧（`assets/turkey-teaser.jpg`）和 release 附带的预告片
包含上面致谢的作品，不在 MIT 许可范围内，仍适用各自的许可（素材为 CC BY 3.0，音乐为 CC BY 4.0）。

<sub>MCP registry: `mcp-name: io.github.Cassette-Editor/oh-my-cassette`</sub>
