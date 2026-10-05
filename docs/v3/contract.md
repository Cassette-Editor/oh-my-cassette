# Cassette MCP 桥接契约 v1

状态：草案（2026-10-02）。关键词 MUST / SHOULD / MAY 按 RFC 2119 理解。

这份契约规定 **本地 bridge（`oh-my-cassette`）** 与 **Cassette MCP 服务** 之间需要事先约定的全部内容。它刻意很小：工具的名字、描述、参数、状态、流程全部由 MCP 服务定义，可以随时改，不需要 bridge 发版。bridge 只认识这里写的东西。

```
宿主（Claude Code / Codex / OpenCode / Hermes）
   │ stdio，标准 MCP
   ▼
oh-my-cassette bridge：转发 · 令牌 · 本地预处理与上传 · 产物下载 · 断线重连
   │ Streamable HTTP（MCP 2026-07-28）+ 本契约
   ▼
Cassette MCP 服务（Cassette-Editor 仓库里的独立进程）：工具定义、说明文字、有界等待
   │ 公开 HTTP API，带调用者自己的 bearer
   ▼
Cassette 后端
```

下文的“后端”指 bridge 连接的 MCP 服务。

## 1. 传输与协议

1. 后端 MUST 以 Streamable HTTP 提供 MCP 服务。bridge 只读 `CASSETTE_MCP_URL`，默认 `http://127.0.0.1:8790/mcp`（本地开发的 MCP 服务），发布前换成生产地址。
2. bridge 与后端之间只用 MCP 2026-07-28 协议（`server/discover` 加逐请求的响应流）。后端 MUST 支持它。只支持握手时代协议（`initialize`，2025-11-25 及更早）的后端会被拒绝，bridge 报 `bridge.protocol_unsupported`。原因是 §7 的语义依赖它：在 2026-07-28 下，离开一次调用会关闭这次请求的响应流，后端借此停止为它等待；握手时代的 `notifications/cancelled` 到不了无状态的后端。宿主与 bridge 之间两代协议都支持。
3. 后端 MUST 提供 `tools` 能力。v1 的 bridge 只转发 tools、`instructions` 和进度通知；resources、prompts、sampling、elicitation、roots 不转发，后端 MUST NOT 依赖它们。需要问用户的地方，用工具结果里的类型化状态表达（例如 `needs_input`）。
4. `structuredContent` MUST 是 JSON 对象。2025 年代的宿主只接受对象。
5. 后端 SHOULD NOT 把业务状态绑定在 MCP 会话上。后端每次部署、网络中断，bridge 都会重新建立会话，新会话必须能看到同一批项目。
6. 鉴权：`oh-my-cassette login --target web|desktop` 使用 PKCE S256 和 loopback 回调取得独立 OAuth grant。Web 通过邮箱验证码登录；Desktop 在本机授权页允许或拒绝连接，无需云账号，可离线配对，授权绑定本地 profile、客户端及设备 resource。access/refresh token 放入系统钥匙串，跨进程锁协调刷新；上游、上传、下载每次发送请求都向同一凭证管理器取 token。MCP 仅获 Agent scope。未登录的 stdio 立即启动并提供连接状态，登录后通知工具列表更新；不支持更新的宿主需要重启。旧桌面云授权需要重新完成本地授权。`CASSETTE_AUTH_TOKEN` 仅保留为高级兼容覆盖，仍接受相同校验。
7. `instructions`：后端的 `instructions` 原样转发给宿主，bridge 不追加内容。工作流原则、路由规则写在这里；宿主侧的 SKILL.md 只保留不随后端变化的原则。
8. 保活：后端 SHOULD 在每个 SSE 响应流上至少每 15 秒发一行 SSE 注释。bridge 的 HTTP 读空闲上限是 60 秒，超过就当作连接已断（§7）；单次调用本身没有总时限。单个 SSE 事件最大 32 MiB。

## 2. 契约声明与版本

后端 SHOULD 在能力里声明本契约：

```json
{
  "capabilities": {
    "extensions": {
      "io.github.cassette-editor/bridge": { "version": 1, "minBridgeVersion": "0.6.0" }
    }
  }
}
```

- bridge 支持的 `version` 集合不包含后端声明的版本，或者 bridge 自身版本低于 `minBridgeVersion` 时，bridge 不转发任何工具，只暴露 `cassette_bridge_status`，提示用户升级（`uvx oh-my-cassette@latest`）。
- 没有声明时，bridge 当作普通 MCP 代理工作；§3、§5 的功能仍按工具和结果上的标注生效。
- v1 内只做**加法**（新增可选字段）。任何不兼容修改都要升到 `version: 2`，bridge 可以同时支持多个版本。

## 3. 本地文件输入

远端服务读不到用户磁盘。需要本地文件的工具，在工具定义的 `_meta` 里标注哪些参数是本地路径，并可以声明上传前怎样预处理：

```json
{
  "name": "cassette_import",
  "inputSchema": {
    "type": "object",
    "properties": {
      "project_id": { "type": "string" },
      "files": { "type": "array", "items": { "type": "string" } }
    },
    "required": ["project_id", "files"]
  },
  "_meta": {
    "io.github.cassette-editor/localFiles": {
      "pointers": ["/files"],
      "accept": ["video/*", "audio/*", "image/*"],
      "prepare": {
        "video": {
          "renditions": [
            {
              "role": "canonical", "container": "mp4", "mimeType": "video/mp4",
              "video": { "codec": "h264", "profile": "high", "pixelFormat": "yuv420p", "bitDepth": 8,
                         "frameRate": 30, "maxLongEdge": 1920, "maxShortEdge": 1080,
                         "keyframeIntervalSeconds": 2, "crf": 20, "preset": "medium" },
              "audio": { "codec": "aac", "bitrate": 192000, "sampleRate": 48000, "channels": 2 }
            },
            {
              "role": "preview", "container": "mp4", "mimeType": "video/mp4",
              "video": { "codec": "h264", "profile": "high", "pixelFormat": "yuv420p", "bitDepth": 8,
                         "frameRate": 30, "maxLongEdge": 1280, "maxShortEdge": 720,
                         "keyframeIntervalSeconds": 2, "crf": 23, "preset": "fast" },
              "audio": { "codec": "aac", "bitrate": 128000, "sampleRate": 48000, "channels": 2 }
            }
          ]
        },
        "audio": {
          "accept": ["mp3", "wav", "m4a", "aac", "ogg", "oga", "opus", "flac"],
          "convert": { "container": "flac", "mimeType": "audio/flac", "extension": "flac" }
        },
        "image": {
          "accept": ["jpg", "jpeg", "png", "gif", "webp", "bmp", "avif"],
          "convert": { "container": "jpeg", "mimeType": "image/jpeg", "extension": "jpg", "quality": 92 }
        }
      }
    }
  }
}
```

- `pointers`：指向 `arguments` 的 JSON Pointer（RFC 6901）。目标在 `inputSchema` 里 MUST 是 `string`，或者元素为 `string` 的 `array`。v1 不支持通配符。
- `accept`：可接受的 MIME 模式。bridge 取它和本地策略的交集。本地策略默认只允许 `video/*`、`audio/*`、`image/*`；用户可以设 `CASSETTE_UPLOAD_ANY_TYPE=1` 放宽。**后端无法放宽本地策略**，这是防止借提示注入外传本地文件的底线。
- `prepare`：可选，见 §3.2。后端从自己的媒体预处理规格生成它。

bridge 对每个路径依次做这些事：

1. 相对路径按工作区根目录解析（工作区默认是 bridge 的启动目录，Claude Code 和 Codex 里就是项目目录），然后解析符号链接。
2. 解析后的路径 MUST 是普通文件，并且位于工作区或 `CASSETTE_ALLOWED_ROOTS` 之内。这可以挡住 `../../.ssh/id_rsa` 这类路径和越界的符号链接。
3. 文件大小不超过 `CASSETTE_MAX_UPLOAD_MB`（默认 4096），MIME 类型（按扩展名推断）在允许范围内。
4. 按 §3.2 预处理，按 §3.1 上传，再把参数里的每个路径**替换成后端返回的 `ref` 字符串**，然后转发原调用。同一次调用里重复出现的同一个文件只处理一次。预处理和上传（“本地阶段”）在后台运行，一次调用最多等它 `CASSETTE_LOCAL_WAIT_SEC` 秒，见 §7。

后端只会收到 `ref`，永远看不到本地路径。任何一步失败，bridge 都不会转发原调用，而是返回 §6 定义的错误。

声明了 `localFiles` 的工具可能收到 bridge 自己的 `preparing` 结果（§7），它不符合工具的 `outputSchema`。所以 bridge 把这类工具列给宿主时去掉 `outputSchema`；其他工具原样列出。

### 3.1 上传握手

后端提供两个“bridge 专用工具”，在工具的 `_meta` 里标注角色：

| `_meta["io.github.cassette-editor/bridge"]` | 建议的名字 |
|---|---|
| `"upload.begin"` | `cassette_upload_begin` |
| `"upload.complete"` | `cassette_upload_complete` |

bridge 会从宿主看到的工具列表里隐藏这两个工具。不经过 bridge、直接连 MCP 服务的客户端仍然看得到它们，所以它们的描述 SHOULD 写明“供本地 bridge 使用”。

**`upload.begin`**，输入：

```json
{
  "tool": "cassette_import",
  "arguments": { "project_id": "…", "files": ["f0", "f1"] },
  "files": [
    {
      "clientRef": "f0",
      "name": "clip.mkv",
      "relativePath": "clips/clip.mkv",
      "size": 123,
      "sha256": "<源文件的 sha256>",
      "mimeType": "video/x-matroska",
      "artifacts": [
        { "role": "canonical", "size": 1, "sha256": "…", "mimeType": "video/mp4" },
        { "role": "preview", "size": 1, "sha256": "…", "mimeType": "video/mp4" }
      ],
      "preparation": { "…": "§3.3 的报告" }
    },
    {
      "clientRef": "f1",
      "name": "photo.jpg",
      "relativePath": "photos/photo.heic",
      "size": 456,
      "sha256": "<转换后文件的 sha256>",
      "mimeType": "image/jpeg",
      "artifacts": [ { "role": "original", "size": 456, "sha256": "<同上>", "mimeType": "image/jpeg" } ],
      "preparation": null
    }
  ]
}
```

- `arguments`：原调用的参数，每个本地路径换成它的 `clientRef`。后端从这里读项目 id 等上下文。
- `name`、`size`、`sha256`、`mimeType` 是这个文件的内容身份：视频是**源文件**的（源文件本身不上传）；转换过的音频和图片是转换后文件的；原样上传的就是文件本身。`relativePath` 总是源文件相对工作区（或允许目录）的路径。
- `artifacts`：要上传的文件。视频是 `prepare.video.renditions` 的每个角色；其余是一个 `original`。
- `preparation`：视频的预处理报告（§3.3），其余为 `null`。

`structuredContent` 输出：

```json
{
  "uploads": [
    {
      "clientRef": "f0",
      "uploadId": "<uuid>",
      "puts": [
        { "role": "canonical", "url": "https://…", "headers": { "Content-Type": "video/mp4" } },
        { "role": "preview", "url": "https://…", "headers": { "Content-Type": "video/mp4" } }
      ]
    },
    { "clientRef": "f1", "ref": "<已有素材的 id>" }
  ]
}
```

- 每个 `clientRef` MUST 恰好对应一个条目。
- 带 `ref`、不带 `uploadId` 的条目表示后端已经有相同内容（按 `sha256` 去重）：bridge 不上传，也不把它交给 `upload.complete`。
- 带 `uploadId` 的条目 MUST 给每个产物角色恰好一个 `puts` 目标。bridge 把产物文件 PUT 到对应 URL，请求头就是给定的 `headers`，再加 `Content-Length`。预签名绑定了 `Content-Type`，bridge 不改动给定的头。
- `url` MUST 自带授权（预签名 URL）。只有当它和 `CASSETTE_MCP_URL` 同源时，bridge 才附带 `Authorization` 头；否则不带任何凭证。
- v1 只做单次 PUT，不支持超过 5 GB 的产物。分片上传以后作为可选字段加入。

**`upload.complete`**，输入：

```json
{ "uploadIds": ["<uuid>"] }
```

`structuredContent` 输出：

```json
{ "files": [ { "uploadId": "<uuid>", "ref": "media:6c84…" } ] }
```

- 单个文件失败时，对应条目返回 `{"uploadId": "…", "error": {"code": "…", "message": "…"}}`，bridge 让整个调用以 `bridge.upload_failed` 失败。
- 之后 bridge 把参数里的路径换成 `ref` 并转发原调用。目标工具要不要等素材就绪由后端决定，等待按 §7 的有界等待处理。

### 3.2 预处理声明

`prepare` 下每类媒体各有一节，文件的类型由 MIME 前缀决定：

- `video/*`：**总是**转码成 `video.renditions` 里的每一个产物，原文件从不上传。
- `audio/*`、`image/*`：扩展名（不分大小写）在 `accept` 里的，原样作为 `original` 上传；否则先转换成 `convert` 描述的格式，上传的是转换后的文件。它的名字是源文件名去掉扩展名再加 `.` + `extension`（`photo.heic` → `photo.jpg`），`relativePath` 仍是源文件的。
- 没有 `prepare`，或 `prepare` 里没有这类媒体的一节：原样作为 `original` 上传。

| 字段 | 含义 | bridge 的做法（ffmpeg） |
|---|---|---|
| `role` | 产物角色，各不相同，不能是 `original` | 上传时的 `artifacts[].role` / `puts[].role` |
| `container`、`mimeType` | 产物容器和 MIME（v1：`mp4`） | `-f mp4 -movflags +faststart` |
| `video.codec`、`profile` | v1：`h264`；`high` / `main` / `baseline` | `-c:v libx264 -profile:v` |
| `video.pixelFormat`、`bitDepth` | 像素格式，v1 位深为 8 | `format=` 滤镜与 `-pix_fmt` |
| `video.frameRate` | 恒定帧率 | `fps=` 滤镜、`-fps_mode cfr -r` |
| `video.maxLongEdge`、`maxShortEdge` | 显示尺寸的长边、短边上限 | 等比缩放进框内，偶数尺寸，从不放大 |
| `video.keyframeIntervalSeconds` | 关键帧间隔 | `-g` 和 `-keyint_min` = 帧率 × 间隔，`-sc_threshold 0` |
| `video.crf`、`preset` | 质量和速度（0–51；x264 的 preset 名） | `-crf`、`-preset` |
| `audio.codec`、`bitrate`、`sampleRate`、`channels` | v1：`aac` | 只有源文件有音频时才输出音轨 |
| `accept` | 原样上传的扩展名 | |
| `convert.container`、`mimeType`、`extension` | 转换目标；v1：音频 `flac`，图片 `jpeg` | 音频 `-c:a flac`；图片取第一帧 `-c:v mjpeg` |
| `convert.quality` | JPEG 质量 0–100 | 映射到 `-q:v`（100 → 2，0 → 31） |

- 未知的键忽略，词汇表只做加法。格式不对，或者要求了 bridge 不会产出的值（上表里的 v1 取值之外），按 `bridge.contract_violation` 处理。
- 视频用**一次** ffmpeg 产出全部产物：解码一次，`split` 分给每个产物，所以各产物帧数相同。旋转元数据先应用到画面上（autorotate），产物不再带旋转；非方形像素拉成方形。HDR 源（PQ/HLG）在 ffmpeg 带 zscale 时色调映射到 SDR BT.709，否则至少把矩阵转成 BT.709 并输出 8 位 yuv420p。产物统一标为 BT.709、limited range。
- 图片转换尊重 ffmpeg 能读到的 EXIF 方向；音频转换只取第一条音轨。
- ffmpeg 和 ffprobe 取自 `CASSETTE_FFMPEG` / `CASSETTE_FFPROBE`，否则从 `PATH` 查找。找不到时报 `bridge.ffmpeg_unavailable`；ffmpeg 或 ffprobe 失败时报 `bridge.prepare_failed`，消息带 stderr 的最后不超过 1000 个字符。
- 中间文件写在 `CASSETTE_TEMP_DIR`（默认系统临时目录下的 `oh-my-cassette/`）里，上传完成或失败后删除。

### 3.3 预处理报告

视频的 `preparation` 字段是下面这个对象，后端用 zod 校验：

```json
{
  "recordingClock": null,
  "source": "PROFILE（container 取 ffprobe 格式名的第一段，如 matroska、mov、mpegts）",
  "canonical": "PROFILE（container 为 mp4）",
  "preview": "PROFILE（container 为 mp4）",
  "canonicalWasTranscoded": true,
  "preparedAt": "2026-10-02T08:00:00.000Z",
  "processor": { "name": "ffmpeg", "version": "<ffmpeg 版本号，不超过 64 字符>", "encoder": "libx264" }
}
```

每个产物角色一个键（按 v1 声明就是 `canonical` 和 `preview`）。`PROFILE` 由 ffprobe 读出：

| 字段 | 类型 | 含义 |
|---|---|---|
| `container` | string | 见上 |
| `codec` | string | ffprobe 的视频编码名（`h264`、`hevc`…） |
| `codecParameter` | string \| null | H.264 的 RFC 6381 编码串（`avc1.640028`），其余为 null |
| `displayWidth`、`displayHeight` | 正整数 | 显示尺寸：应用旋转和像素宽高比之后 |
| `rotation` | 整数 | 顺时针旋转角度（0/90/180/270）；产物为 0 |
| `durationSeconds` | 正数 | 时长 |
| `frameRate` | number \| null | 平均帧率，没有时取 `r_frame_rate` |
| `frameRateIsConstant` | bool | `r_frame_rate` 与 `avg_frame_rate` 相差不超过 0.02 |
| `hdr` | bool | `color_transfer` 是 `smpte2084` 或 `arib-std-b67` |
| `bitDepth` | 正整数 \| null | `bits_per_raw_sample`，否则由像素格式推断 |
| `pixelFormat` | string \| null | ffprobe 的 `pix_fmt` |
| `hasAudio` | bool | 是否有音轨 |
| `audioCodec`、`audioSampleRate`、`audioChannels` | string / 正整数 / 正整数，或 null | 第一条音轨 |

`recordingClock`：只有 ffprobe 在格式或任一流上报告了 `timecode` 标签时才有值，否则为 `null`：

```json
{
  "origin": "embedded-timecode",
  "ticks": 107894,
  "ticksPerSecond": { "num": 30000, "den": 1001 },
  "sourceOriginUs": 0,
  "wrapsAt24Hours": true,
  "dropFrame": true,
  "nominalFps": 30
}
```

- `ticksPerSecond` 是视频流 `r_frame_rate` 的最简分数；`nominalFps` 是它四舍五入后的整数。
- `ticks` 是时间码在名义帧率下距 `00:00:00:00` 的帧数。分隔符为 `;` 且名义帧率是 30 的倍数时按丢帧计数：每分钟开头跳过 2 个编号（60 fps 时 4 个），逢十的分钟不跳，这时 `dropFrame` 为 true。例子里的时间码是 `01:00:00;02`。

## 4. 工作区上下文

bridge 在每个 `tools/call` 的 `params._meta` 里加上：

```json
{
  "io.github.cassette-editor/workspace": { "id": "3f1c…（32 位十六进制）", "name": "my-vlog" },
  "io.github.cassette-editor/host": { "name": "claude-code", "version": "2.1.0" },
  "io.github.cassette-editor/elapsedSeconds": 12.5
}
```

- `workspace.id` 是工作区真实路径的 sha256 前 32 位。它在同一台机器的同一个目录下保持不变，不是秘密，也不是凭证。
- 后端 MAY 用它记住“这个目录最近用的是哪个项目”。后端仍 MUST 接受显式传入的项目 id。
- `host` 取自宿主的 `clientInfo`，只用于统计和排错，不能用来区分行为或做安全判断。
- `elapsedSeconds` 是 bridge 在转发之前已经为这次宿主调用花掉的秒数（主要是本地阶段）。后端 SHOULD 把它从自己的有界等待窗口里扣掉，让整次调用留在宿主的超时之内。

## 5. 产物下载

需要把文件交回用户磁盘的结果（例如导出），在结果的 `_meta` 里声明：

```json
{
  "structuredContent": { "status": "completed", "file": "https://…signed…" },
  "_meta": {
    "io.github.cassette-editor/downloads": [
      {
        "url": "https://…signed…",
        "fileName": "final.mp4",
        "size": 73400320,
        "sha256": "…",
        "pointer": "/file"
      }
    ]
  }
}
```

- bridge 把文件下载到 `CASSETTE_DOWNLOAD_DIR`（默认是 `<工作区>/cassette-exports/`）。文件名会经过清理，已存在时自动加后缀，不会覆盖。
- 给了 `size`、`sha256` 就校验，不一致时删除文件并报告失败。
- 下载成功后，bridge 在结果里追加一条文本，写明本地路径。给了 `pointer` 时，还会把 `structuredContent` 里该字段的值换成本地绝对路径，所以这个字段在 `outputSchema` 里 MUST 是普通字符串（不要加 `format: uri`）。
- URL 只允许 http(s)，MUST 自带授权；只有和 `CASSETTE_MCP_URL` 同源时才附带 bearer。
- 下载失败时，bridge 保留原结果，追加一条说明失败原因和原始 URL 的文本，不改动 `pointer` 指向的字段。

## 6. 错误

- 业务错误由后端自己定义。建议格式：`isError: true`，`structuredContent` 为 `{"error": {"code", "message", "retryable"}}`。bridge 不解释它们。
- bridge 自己产生的错误同样用 `isError: true` 返回，`structuredContent` 为 `{"error": {"code": "bridge.*", "message", "retryable"}}`：

| code | 含义 | retryable |
|---|---|---|
| `bridge.backend_unreachable` | 连不上后端，或连接在调用中断开（后端的工作可能还在继续，见 §7） | 是 |
| `bridge.forbidden` | HTTP 403：当前账号、授权或目标不允许访问 | 否 |
| `bridge.auth_unavailable` | 身份服务或系统钥匙串暂不可用，保留凭证后重试 | 是 |
| `bridge.unauthorized` | 后端回答 HTTP 401：没有令牌，或令牌不被接受 | 否 |
| `bridge.upgrade_required` | 后端要求更高的 bridge 版本或契约版本 | 否 |
| `bridge.protocol_unsupported` | 后端只支持握手时代的协议，不支持 MCP 2026-07-28（§1） | 否 |
| `bridge.file_not_found` | 本地路径不存在，或不是普通文件 | 否 |
| `bridge.file_outside_workspace` | 路径在工作区和允许目录之外 | 否 |
| `bridge.file_type_rejected` | MIME 类型不在 `accept` 与本地策略的交集内 | 否 |
| `bridge.file_too_large` | 超过 `CASSETTE_MAX_UPLOAD_MB` | 否 |
| `bridge.ffmpeg_unavailable` | 需要预处理，但找不到 ffmpeg 或 ffprobe；消息说明怎样安装，或设置 `CASSETTE_FFMPEG` / `CASSETTE_FFPROBE` | 否 |
| `bridge.prepare_failed` | ffmpeg 或 ffprobe 处理失败；消息带 stderr 的最后不超过 1000 个字符 | 否 |
| `bridge.upload_failed` | 上传握手、PUT 或 `complete` 失败 | 是 |
| `bridge.invalid_argument` | `localFiles` 指向的参数不是路径字符串或字符串数组 | 否 |
| `bridge.contract_violation` | 后端的标注不合法（例如 `prepare` 格式不对，或声明了 `localFiles` 却没有上传工具） | 否 |

`bridge.download_failed`（下载失败、大小或 sha256 不符）不会让调用失败，只出现在 §5 所说的追加文本里。

`cassette_bridge_status` 的结果里，`error_code` 是上表中说明当前为什么没有转发工具的那个代码（`bridge.backend_unreachable`、`bridge.unauthorized`、`bridge.protocol_unsupported` 或 `bridge.upgrade_required`），连接正常时为 `null`。

## 7. 长任务、取消和工具列表变化

- **有界等待**：可能耗时的工具 SHOULD 只等待一个有界窗口（约 240 秒，再扣掉 `elapsedSeconds`）。窗口内没有结束时，返回**非错误**的类型化结果（例如 `running` / `processing`），带上用来续等的 id，并说明再调用一次（同一个 id，或同样的参数）就是继续等。续等是正常结果，不是错误。宿主的工具超时按 600 秒配置，本地阶段和后端各自一个窗口都在这之内。
- **进度**：等待期间后端 SHOULD 至少每 20 秒发一次 `notifications/progress`（宿主会把它当作保活信号），bridge 原样转发。按 MCP 规范，`progress` 的值要单调递增。
- **断线只脱离**：连接或响应流断开时，后端 MUST NOT 因此取消正在进行的工作。bridge 对这次调用报 `bridge.backend_unreachable`（可重试），消息说明后端的工作可能还在运行，再次调用即可接上。
- **取消只表示不再等待**：宿主取消调用时（无论宿主用哪一代协议），bridge 关闭这次请求在后端的响应流。它的含义是“不再等待”，不是“停止工作”；停止工作由单独的工具负责。
- **bridge 自己的本地阶段遵循同一模式**：
  - 预处理加上传在后台任务里运行，任务的键是（工具名、`prepare` 声明的摘要、把路径换成 `clientRef` 后的参数、每个文件的真实路径 + `mtime_ns` + 大小）。
  - 一次宿主调用最多等 `CASSETTE_LOCAL_WAIT_SEC`（默认 240）秒。没完成时返回**非错误**结果：`structuredContent` 为 `{"status": "preparing", "progress": {"done": 0, "total": 2, "files": [{"path": "clips/a.mkv", "stage": "transcoding", "percent": 42.0}]}}`（`stage` 取 `transcoding` / `uploading` / `done`），文本说明文件仍在本地准备、工作在后台继续，用同样的参数再调用同一个工具就是继续等。
  - 再次调用会接上同一个任务，不会重复转码。等待期间 bridge 向宿主发进度通知：最多每秒一条，没有变化时也至少每 20 秒一条。
  - 本地阶段在窗口内完成时，bridge 带着 `ref` 和 `elapsedSeconds` 转发原调用。完成的结果（`ref`）在 bridge 进程的生命周期内缓存，同样的调用直接转发。
  - 失败的任务只报告一次（对应的 `bridge.*` 错误），然后被丢弃，下次调用重新开始。
- **工具列表变化**：后端声明 `tools.listChanged` 时，bridge 订阅 `subscriptions/listen`，收到变化后重新拉取工具列表并通知宿主（握手时代的宿主收到 `notifications/tools/list_changed`，2026-07-28 的宿主通过它自己的 `subscriptions/listen` 收到）。后端重新部署后，bridge 重连成功时也会通知宿主。

## 8. 一致性检查

```bash
uvx oh-my-cassette check --url http://127.0.0.1:8790/mcp --token "$CASSETTE_AUTH_TOKEN" --upload \
  --arguments '{"project_id": "<一个测试项目>"}'
```

它检查以下各项，有 FAIL 时退出码为 1：

- 能否用 MCP 2026-07-28 连上（只支持握手时代协议的后端判 FAIL）；回答 401 时单独说明是缺令牌还是令牌被拒；
- 契约声明是否存在、版本是否受支持；
- 是否有 `instructions`；
- 至少有一个宿主可见的工具，且每个工具的 `inputSchema` 根类型是 object；
- `localFiles` 的 pointer 是否指向 `string` 或 `string[]` 参数；
- `prepare` 声明是否合法（§3.2 的词汇和 v1 取值）；
- 声明了 `localFiles` 时，两个上传工具是否都在；
- 加 `--upload` 时，实际跑一遍上传握手：以一个 1×1 PNG 作为 `original` 产物调用 `upload.begin`（`arguments` 取 `--arguments`，并在第一个本地文件 pointer 处放上探针的 `clientRef`，`preparation` 为 `null`），期望得到 `ref`（已有内容），或者 `uploadId` 加上恰好一个角色为 `original` 的 `puts` 目标；按给定的头 PUT，再调用 `upload.complete` 拿到 `ref`。

建议 Cassette-Editor 在 CI 里对本地栈运行这条命令：MCP 服务改坏契约时，在它的 PR 上就会失败，而不是等用户使用时才发现。
