# WeChat Native

让 AstrBot 通过 **agent-wechat-native /v1 API** 接入个人微信。插件装在 AstrBot 内，不需要额外 Bridge 服务。

支持文本私聊、发送图片、微信语音收发、图片识别、PDF 解析、群聊 @回复、群聊摘要、原生引用回复、白名单、独立会话、断线重连和持久化去重。
支持开启机器人的**文本转图片**功能；普通文件发送暂不支持。

适配版本：**AstrBot >=4.29.3,<5、Native v1.4.0**；插件运行需要 **Python 3.12+**。

## 安装与配置

1. 启动 Native 并登录微信；全新 Docker 部署按下一节操作。
2. 在 AstrBot **插件管理 → 从链接安装**，输入：
   `https://github.com/neozhu/astrbot-plugin-wechat-native`。
3. 在 **机器人 → 新增机器人** 中选择 **WeChat Native**，填写下表并启用。
4. 为机器人选择 AstrBot 配置文件，配置模型和人格，再测试私聊及群 @回复。

参数配置示例（Account ID 和开关以实际使用为准）：

![WeChat Native 机器人参数配置](docs/wechat-nactive.png)

### 连接信息

以下字段填写在 AstrBot 的机器人表单中，**不是 Native 的 server.toml 配置**。

| 字段 | 填写说明 | 本文 Docker 示例 |
| --- | --- | --- |
| `server_url` | AstrBot 能访问的 Native 地址，可带 `/v1` | `http://127.0.0.1:6174` |
| `account_id` | Native 的账号 `id`，从 `wx accounts list` 获取；不是微信号 | 替换为实际 ID |
| `token_file` | Token 文件的**容器内路径**，优先于 `token` | `/run/secrets/wechat-api-token` |
| `token` | 不用文件时填写 Native API Token，会保存在 AstrBot 配置里 | 使用文件时留空 |

Native Token 需要目标账号的 **read + send + media** 权限。本文 Compose 让两个容器读取同一份 Token 文件。
模型 API Key 在 AstrBot 的模型设置中配置；更换 Token 文件后重启机器人。

### 回复规则

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `enable_private_reply` | `true` | 是否回复私聊 |
| `enable_media` | `true` | 启用图片、PDF 与语音接收；群聊支持引用媒体 + @或先发送再 @ |
| `allowed_users` | `[]` | 私聊 senderId 白名单；空列表允许所有人，不限制群成员 |
| `group_reply_mode` | `mention` | `mention`：真正 @机器人；`all`：全部群文本与直接语音都直接唤醒；`probability`：全部进入 AstrBot，但只有 @ 唤醒，其余交给 AstrBot 的主动回复概率；`disabled`：不回复群聊 |
| `group_reply_style` | `mention` | 群聊回复方式：`mention` @触发者；`quote` 引用触发消息；`plain` 直接发送 |
| `allowed_groups` | `[]` | 群 threadId 白名单，通常以 `@chatroom` 结尾；空列表允许所有群 |
| `unique_session` | `true` | 同群不同成员独立会话；关闭后同群共享会话 |

用户 ID 和群 ID 可用 Native 的 `wx contacts find 名字`、`wx chats list` 查询。
手打 `@名字` 或仅 @全体不会触发默认群回复。
在 **机器人 → WeChat Native → 群聊回复方式** 下拉框中，可选择 **@触发者 / 引用触发消息 / 直接发送**，默认保留 @触发者。
该设置只控制回复样式，不改变群聊的 @唤醒规则。分段回复仅首段自动 @或引用；纯图片/语音直接发送。
插件或 AstrBot 全局回复装饰已显式添加的 @、引用仍保留；若希望完全由此选项控制，请关闭 AstrBot 全局的“@回复”和“引用回复”。
自动 @所需的群成员资料刷新失败时，会省略该自动 @并继续回复正文；显式 @仍需资料可用。
AstrBot 的模型开关、全局白名单和插件权限仍需正确配置。

### 支持发送图片

AstrBot 的 `Image` 组件可发送到微信私聊和群聊，也支持通过已保存会话主动发送。
可使用 `Image.fromBytes(data)`、`Image.fromFileSystem(path)`、`Image.fromURL(url)`、
`Image.fromBase64(data)`、`Image.fromIO(stream)`，以及标准 `Image` 的本地路径、file URI、
base64 data URI 或裸 base64 来源；`url` 字段优先于 `file`，与 AstrBot 的接口一致。
支持 JPEG、PNG、GIF、WebP，**单张上限 10 MiB**；Native 负责后续图片处理，微信端显示效果需实际验证。

文本和图片按消息链顺序逐段发送，多张图片逐张确认后再发送下一张。例如：

```python
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image, Plain

await event.send(MessageChain([
    Plain("第一张图"),
    Image.fromFileSystem("/app/data/example.jpg"),
    Plain("第二张图"),
    Image.fromBytes(image_bytes),
]))
```

插件先通过 `POST /v1/accounts/{account}/uploads` 上传图片，再通过 `/sends` 提交
`kind=image`、`sourceId` 和 `Idempotency-Key`，不会把图片作为普通文件发送。
图片 job ID 会在轮询前保存到 SQLite。提交结果不明时只用原幂等键查询回执，
后续只查询原 job；不会自动重新上传、重发或换键重试。
失败、超时、断线、5xx 或未知状态会保留 `failed` / `needs_review` / `partial` 状态，
停止发送消息链剩余部分；已发送部分不会回滚，需检查微信和状态库后人工处理。

Native v1.4.0 的图片请求不接受引用和 @字段。混合消息链的显式引用、@和首段自动 @
由第一段非空文本承载；纯图片回复不自动 @。纯图片链若显式包含 `Reply` 或群 @，
需添加非空文本，否则插件在发送前报错。

URL 图片仅允许公网 HTTP(S) 和 80/443 端口，不携带 Native Token、Cookie 或环境代理。
连接时校验实际 DNS 地址，拒绝内网、环回、链路本地及其他非公网地址；最多跟随 3 次重定向，
每一跳重新检查。下载总超时 15 秒、连接及连续读取超时各 5 秒，流式限制 10 MiB，
拒绝压缩传输。内网图床请改为本地路径或 bytes 来源。

### 微信语音收发

**接收语音**使用现有 `enable_media` 开关（默认开启）。插件识别微信 `type=34`，
通过 Native Media API 获取 `normal` 变体，由 Native 将 SILK 解码为 **16 kHz、单声道、16 位 PCM WAV**。
校验账号、消息、资产身份、大小和 SHA-256 后，生成标准 AstrBot `Record`，
由 AstrBot 现有 ASR 流程识别；插件不调用特定语音服务，也不会把微信语音 XML 当作用户文字。

1. 在机器人所使用的 AstrBot 配置中开启**语音转文字/ASR**（`provider_stt_settings.enable`），
   并为该会话选择 ASR 提供商；也可使用能处理标准 `Record` 的 ASR 插件。
2. 私聊直接发送微信语音即可，仍遵守私聊开关和 `allowed_users`。
3. 默认 `mention` 群聊模式：先发语音，再由同一成员在 **2 分钟内真正 @机器人**发送
   `听听刚才的语音`、`识别这段录音`，或只 @机器人；也可引用语音并真正 @机器人提问。
   普通群语音仅临时保存消息标识，不会提前下载或识别。
4. `all` 模式允许直接处理群语音；`disabled`、`allowed_groups` 和成员独立会话规则照常生效。
   引用语音仍要求真正 @机器人。引用原消息必须已记录在同一账号、同一聊天，其他成员的语音请明确引用。

没有配置 ASR 时，插件仍交付 `Record`，不会自行选择服务商或捏造转写结果；
AI 是否能理解音频取决于 AstrBot 的 ASR、插件或模型配置。群聊摘要仍只收集原有文本，不收集语音转写。
撤回会取消待处理任务；下载前后及发送回复前会检查来源身份和撤回状态。
媒体下载默认总超时 60 秒，接收的单条微信语音上限 60 秒；音频内容不写入插件 SQLite。
交付后的临时 WAV 由 AstrBot 标准媒体流程创建并管理。

**发送语音**支持 AstrBot TTS/插件产生的标准 `Record`：本地路径、file URI、
`Record.fromFileSystem(path)`、`Record.fromURL(url)`、`Record.fromBase64(data)`、
音频 base64 data URI 和裸 base64；遵循 `Record` 的 `file → url → path` 来源回退。
同一 AstrBot 内的 TTS 建议设置 `provider_tts_settings.use_file_service: false`，直接使用生成的本地音频路径。
例如：

```python
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain, Record

await event.send(MessageChain([
    Plain("这是语音回复："),
    Record.fromFileSystem("/app/data/tts-output.wav"),
]))
```

插件先 `/uploads`，再 `/sends` 提交 **`kind=voice`**，发送原生微信语音，绝不降级为普通文件。
支持私聊、群聊、主动发送，以及与文本、图片混合的消息链顺序。
语音本身不支持 Native 引用/@字段；需要这些信息时在消息链中添加非空文本，由首段文本承载。
纯语音回复不自动 @；TTS 的 `Record.text` 只是附带元数据，不会额外重复发送成文字。

支持 Native 的 WAV、MP3、FLAC、Ogg、AAC、M4A/MP4、WebM 和 SILK 来源，
容器内音频编码能否解码由 Native 判断。**每个来源最多 26,214,383 字节、总长 300 秒**；
Native 负责格式转换和内置 SILK 编码，并将超过 60 秒的音频拆为尽量等长、每条不超过 60 秒的微信语音。
建议在 **Native 所在主机/容器**安装 FFmpeg，以处理压缩音频及其他采样格式；
16 kHz 单声道 PCM16 WAV 和支持的 SILK 可使用 Native 内置处理路径。插件不自行切片或重复编码。

一个 `Record` 对应一次上传、一个幂等键和一个 Native job，默认最多等待 25 分钟。
逐段 `index/state/durationMs/serverMessageId` 回执存入 SQLite 的 `send_chunks`，
与原 `sends` 表的 job ID 关联；旧文本和图片状态无需迁移或清空。
长语音部分成功后发生失败、超时、断线或未知状态，会保留已接受片段的 ID，
消息标记 `partial`，不确定的 job 保留 `needs_review`，并停止消息链剩余部分。
提交结果不明时只查询原幂等键/原 job，不会自动重发、重新拆分或重试剩余语音片段。
重启继续保留这些回执供人工核查。Native `accepted` 不代表已在收件端成功播放，实际微信效果需实测。

URL 音频复用图片的公网 HTTP(S)、80/443 端口、DNS/SSRF 校验和最多 3 次重定向限制，
下载总超时 15 秒、连接/连续读取超时各 5 秒，按上述音频大小上限流式读取；
不携带 Native Token、Cookie 或环境代理，也不接受压缩传输。

### 图片识别

私聊直接发送图片即可。群聊支持两种方式：

- 引用需要分析的图片，再真正 @机器人提问。
- 先发图片，再在 **2 分钟内**由同一成员真正 @机器人发送 `分析刚才的图片`、`看看这张图`，或只 @机器人使用默认描述提示词。

第二种方式只关联同一群、同一发送者的最近一张图片；其他成员的图片需要明确引用。
普通发图只临时记录消息标识，收到看图请求时才下载，重启后需重新发图或使用引用。
引用媒体遇到 Native v1.3.3 的 `409 media_metadata_invalid` 时，会尝试读取同一聊天中已记录的原图，
并在下载前后及发送回复前重新检查身份和撤回状态。详细规则见[图片识别说明](docs/image-recognition.md)。

### 群聊摘要

在 WeChat Native **机器人参数**中开启 `enable_group_summary`，保持
`group_reply_mode: mention`。群里正常聊天后，**真正 @机器人**发送 `总结群聊`
或 `群聊摘要`，机器人会使用当前 AstrBot 会话的聊天模型整理讨论要点、结论、待办及未解决问题。
普通群消息仅进入摘要缓存，不会因此触发回复；`all` 模式仍按原规则处理所有群文本。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `enable_group_summary` | `false` | 开启后在内存缓存允许群的实时文本，供手动生成摘要 |
| `summary_max_messages` | `200` | 每群缓存条数，范围 1–500 |
| `summary_window_minutes` | `60` | 摘要时间范围，单位分钟，范围 1–1440 |

缓存遵守 `allowed_groups`，`disabled` 模式不收集群消息；摘要命令仍经过 AstrBot 的全局白名单和插件权限检查。
仅包含开启功能后接收到的实时文本，不补取历史、不包含图片/语音、本人发送或已撤回的消息。
每个机器人所有群合计最多缓存 2000 条，每条最多保留 1000 字符；模型输入不超过 24000 字符，
超限取最新记录，摘要会注明输入范围及截断情况。

聊天正文仅缓存在内存，重启或停止机器人即清空；过期记录在后续收集或读取缓存时清理。
触发摘要会把该群选中的记录发送给当前会话配置的模型服务，独立于日常对话历史。
同群一次只生成一个摘要，60 秒超时会提示重试；无缓存或模型不可用时返回提示。
生成或发送前发现来源记录撤回、过期或被淘汰，会取消旧摘要，需重新触发。

### 消息引用回复

在机器人使用的 **AstrBot 配置文件**中开启 `platform_settings.reply_with_quote`（引用回复），
文本回复就会引用触发它的原消息，私聊和群聊均适用。插件也支持消息链中的
`Reply(id=原消息的 Native id)`；引用目标必须已保存在同一 Native 账号、同一聊天中。

用户发来的引用消息会把评论及可用的内嵌引用文本交给 AstrBot，仍遵守群 @和白名单规则。
引用内容已撤回、缺失或类型不支持时仅提供状态提示，不读取历史来补取内容。
摘要缓存仅保留引用消息本身的评论，不展开内嵌原文。

Native v1.3.3 支持引用回复同时 @群成员或 @全体；群内首段回复按 `group_reply_style` 自动 @、引用或直接发送，
消息链中的显式 @会保留。引用回复正文限 **16 KiB UTF-8**（包括 Native 解析后的 @名称）；不支持的来源由 Native 拒绝，
不会自动降级成普通文本重发。发送前会检查来源身份及撤回状态，沿用原有回执和去重机制。

Native 的原生引用仍属实验能力，服务端接受不等于已在手机正确显示，需要在实际微信中验证。
支持的原消息类型与限制见 [Native 引用回复说明](https://github.com/thisnick/agent-wechat/blob/v1.3.3/docs/src/content/docs/use/cli.mdx#quoted-replies)。

### 图片识别

开启 `enable_media`（默认开启）并为机器人配置支持视觉（Vision）的模型后，支持以下图片分析场景：

- **私聊直接发图**：直接发送图片，未附带文字时默认使用提示词 `请描述这张图片的内容。`。
- **私聊引用图片**：引用已收到的图片并提问，按用户问题结合图片内容回复。
- **群聊引用图片并 @机器人**：引用群内图片并**真正 @机器人**提问（仅 @机器人未附文字时使用默认提示词）。群内直接发图或未 @机器人的引用图片不会自动下载或触发分析，`all` 模式同样要求真正 @机器人；群聊摘要命令优先于引用图片下载。

图片通过 Native Media API 异步拉取（需 Token 具备 `media` 权限），单图上限 10 MiB，超时 60 秒；优先获取高清/原图，WXGF 格式优先使用受支持的预览图。图片已撤回时静默停止，引用图片失效、超限或格式不支持时返回简短提示。实现细节见 [图片识别实现说明](docs/image-recognition.md)。

## Docker 部署：同一台 Linux 主机

需要 Docker Compose 和 OpenSSL。已有部署请在**原 Compose 项目**中修改，保留项目名和数据卷。

两个服务都使用 **host 网络**，因此 AstrBot 可通过 `http://127.0.0.1:6174` 访问 Native，
不配置 `ports`。Native 的默认监听地址也保持为本机地址。

### 1. 准备 Native 配置和 Token

全新部署时先创建工作目录及配置目录：

```bash
mkdir -p wechat-bot
cd wechat-bot
sudo mkdir -p /opt/agent-wechat-native
sudoedit /opt/agent-wechat-native/server.toml
```

在 `server.toml` 中写入：

```toml
schemaVersion = 1
listen = "127.0.0.1:6174"
dataDir = "/var/lib/agent-wechat"
tokenFile = "/run/secrets/wechat-api-token"
```

生成 Token 并设置权限。**已有 Token 会保留，不要覆盖正在使用的凭据。**
Native 容器用户为 UID/GID `10001`；Token 文件保持 `600`，只读挂载。

```bash
sudo sh -c '
  umask 077
  test -f /opt/agent-wechat-native/api-token ||
    openssl rand -hex 32 > /opt/agent-wechat-native/api-token
'
sudo chown 10001:10001 /opt/agent-wechat-native/api-token
sudo chmod 600 /opt/agent-wechat-native/api-token
sudo chmod 644 /opt/agent-wechat-native/server.toml
```

### 2. 创建 compose.yaml

```yaml
services:
  astrbot:
    image: soulter/astrbot:latest
    container_name: astrbot
    restart: unless-stopped
    network_mode: host

    environment:
      TZ: Asia/Shanghai

    volumes:
      - astrbot_data:/AstrBot/data
      - /opt/agent-wechat-native/api-token:/run/secrets/wechat-api-token:ro

    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

  agent-wechat:
    image: ghcr.io/thisnick/agent-wechat-native:v1.4.0
    container_name: agent-wechat
    restart: unless-stopped
    network_mode: host
    user: "10001:10001"
    read_only: true

    cap_drop:
      - ALL

    security_opt:
      - no-new-privileges:true

    environment:
      TZ: Asia/Shanghai
      TMPDIR: /var/cache/agent-wechat

    tmpfs:
      - /tmp:rw,noexec,nosuid,size=64m,mode=1777

    volumes:
      - agent_wechat_native_data:/var/lib/agent-wechat
      - agent_wechat_native_scratch:/var/cache/agent-wechat
      - /opt/agent-wechat-native/server.toml:/etc/agent-wechat/server/config.toml:ro
      - /opt/agent-wechat-native/api-token:/run/secrets/wechat-api-token:ro

    stop_grace_period: 30s

volumes:
  astrbot_data:
  agent_wechat_native_data:
  agent_wechat_native_scratch:
```

`astrbot_data` 保存 AstrBot 配置、会话及插件回执，`agent_wechat_native_data` 保存 Native 账号和登录状态；
升级时保留它们。不要用 `docker compose down -v` 删除登录数据。
`agent_wechat_native_scratch` 是临时快照空间，需要预留磁盘容量。
若自行将 AstrBot 改为非 root 用户，还需保证该用户可读挂载的 Token 文件。

### 3. 启动服务并登录微信

```bash
docker compose config --quiet
docker compose up -d
docker compose logs --tail=50 agent-wechat astrbot
```

首次在 Native 容器内登记本机服务，并查询账号：

```bash
docker compose exec agent-wechat wx config server add local --url http://127.0.0.1:6174 --token-file /run/secrets/wechat-api-token
docker compose exec agent-wechat wx --server-name local accounts list
```

没有账号时创建一个，再按提示扫码或在手机确认。已有账号时使用原账号，不要重复创建。

```bash
docker compose exec agent-wechat wx --server-name local accounts create --alias me
docker compose exec agent-wechat wx --server-name local --account me auth login
docker compose exec agent-wechat wx --server-name local accounts list
```

记下返回的账号 `id`。如需使用已有官方微信资料，按
[Native 登录说明](https://github.com/thisnick/agent-wechat/blob/v1.3.3/docs/src/content/docs/setup/connect.mdx)
挂载资料目录；使用期间保持官方客户端关闭。

### 4. 配置 AstrBot 机器人

打开 `http://宿主机IP:6185`，按启动日志中的初始凭据登录并修改密码。
安装本插件后，在 WeChat Native 机器人表单中填写：

- `server_url`：`http://127.0.0.1:6174`
- `account_id`：上一步返回的实际账号 ID
- `token_file`：`/run/secrets/wechat-api-token`
- `token`：留空

宿主机文件是 `/opt/agent-wechat-native/api-token`，**机器人必须填挂载后的容器内路径**。
确认 AstrBot 版本符合插件要求，配置好模型并启用机器人。管理面板 6185 仅向管理网络开放，Native 6174 保持本机访问。

### 5. 检查连接

进入 **插件管理 → WeChat Native → 可视化视图 → 连接测试**：
“已连接”表示 Native API 验证通过；“在线 / 离线”单独表示微信账号状态。
检测不发送消息，也不验证模型和发送权限，最后还需人工测试私聊及群内真正 @回复。

排查网络时，可从 AstrBot 容器执行只读检查，不输出 Token：

```bash
docker compose exec -T astrbot python - <<'PY'
import json
from pathlib import Path
from urllib.request import Request, urlopen

token = Path("/run/secrets/wechat-api-token").read_text().strip()
req = Request("http://127.0.0.1:6174/v1/status",
              headers={"Authorization": "Bearer " + token})
with urlopen(req, timeout=5) as response:
    print("Native 服务状态:", json.load(response).get("state"))
PY
```

### 其他网络环境

- **Windows / macOS Docker Desktop**：本例的 host 网络需先在设置中启用，文件权限也需按宿主环境调整；见 [Docker host 网络说明](https://docs.docker.com/engine/network/drivers/host/)。
- **Native 在另一台主机**：填 AstrBot 实际可达的 HTTPS 地址或加密隧道入口；Token 文件需在 AstrBot 所在主机另行准备并挂载。
- **AstrBot 使用 bridge 网络**：容器里的 `127.0.0.1` 指向它自己。只加入同一网络或增加端口映射，不能让 Native 的 loopback 监听自动对外可达，需提供受保护的可达入口。

跨主机使用 HTTPS 或受保护隧道；反向代理需关闭 SSE 缓冲并允许长连接。

## 常见问题

| 现象 | 检查方法 |
| --- | --- |
| 没有 WeChat Native 类型 | 确认插件启用、AstrBot 版本符合要求，刷新页面或重启 |
| 401 / 403 | 检查 Native Token、账号范围及 read/send/media 权限 |
| Token 文件不可读 | 核对容器内路径、挂载文件是否存在、运行用户权限 |
| 连接拒绝 / 超时 | 从 AstrBot 容器检查地址、网络模式和 Native 是否启动 |
| API 已连接，微信离线 | 在 Native 完成登录或等待账号恢复连接 |
| 在线但不回复 | 检查模型、白名单和群触发模式；历史或超过 5 分钟的消息会被忽略，注意时钟同步 |
| `needs_review` / `partial` | 保留回执，先核对真实聊天；不要自动重发或删除状态库 |
| HTTP 410 / 游标过期 | 停止机器人并备份状态，由管理员核对 Native 事件保留范围 |

插件状态位于 `/AstrBot/data/plugin_data/astrbot_plugin_wechat_native/`。
请保留整个 AstrBot data 目录；发送回执、重启恢复和只读排查见 [运维说明](docs/operations.md)。

## 开发与协议

```bash
python -m pip install -r requirements-dev.txt
python -m playwright install chromium
python -m pytest -q
ruff check .
ruff format --check .
```

使用 Python 3.12 虚拟环境；生产安装由 AstrBot 处理 `requirements.txt`，无需安装开发依赖。

- [AstrBot Docker 部署](https://docs.astrbot.app/deploy/astrbot/docker.html)
- [Native Docker 部署](https://github.com/thisnick/agent-wechat/tree/v1.3.3/deploy/native)
- [Native v1.4.0 API](https://github.com/thisnick/agent-wechat/blob/v1.3.3/docs/src/content/docs/use/api.mdx)

本插件采用 [MIT 许可证](LICENSE)。仅接入有权操作的微信账号。
