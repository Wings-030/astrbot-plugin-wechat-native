# 运维说明

[返回 README](../README.md)

本页说明消息可靠性、状态保留和回执排查。安装、配置及 Docker 部署请参阅 README。

## 面板扫码登录与跨平台告警（Native v2.3.0）

1. 保持 Native v2.3.0 服务运行，保存并启用 AstrBot 中的 `wechat_native` 机器人。
2. 将既有 Admin Token 文件只读挂载到 `admin_token_file`，默认
   `/run/secrets/wechat-admin-token`；凭据需有目标账号的 `admin` 权限。
   连接检测仍读取原 `token_file`，不会回退用普通 Token 执行登录。
3. 以 AstrBot Dashboard 配置的管理账号登录，打开插件的 WeChat Native 面板。
   未认证时自动准备登录，扫码后在手机确认。过期、拒绝或失败时由用户手动点击
   “重新获取二维码”，直接发起新的扫码登录并跳过旧会话恢复。
4. 在目标平台的管理员私聊发送 `/绑定通知`。多个 Native 实例时需追加 Native 机器人 ID。
   运行 `/测试通知` 后在接收平台确认消息；`/通知状态` 显示最近监控结果及发送尝试情况。
   英文等价命令为 `/bind_alert`、`/unbind_alert`、`/alert_status`、`/test_alert`。

监控在插件 `initialize()` 后运行，每轮并发检查各机器人，单机器人探针不重叠且总超时 5 秒。
连续三次 `offline/unavailable` 才创建事故，连续两次 `authenticated=true + connection=online`
才判定恢复。首次启动在线只建立基线。账号或服务地址变化后重建基线，禁用或移除后停止检查。

Plugin KV 保存绑定、登录幂等 Key/Operation 标识和事故状态。不要在故障排查时随意删除这些记录，
否则会失去重启恢复与去重信息。二维码与 Token 不写 KV；QR 通过 Bridge 传入页面内存，
页面隐藏时暂停轮询并清除图片，重新显示后查询原任务。不要截图分享仍有效的二维码。

通知在调用 `Context.send_message()` 前先写入 attempted 标记；写入失败不发送，
发送失败也不自动重试。没有绑定的事件记录为 `skipped_no_binding`，之后绑定不会补发断线消息；
恢复时如已存在绑定，仍可发送当时的恢复消息。解绑只删除绑定，不删除事故。
仅支持一个活跃 AstrBot 插件实例，多个进程共享 KV 的分布式去重不在本版范围内。

### 排错

扫码故障排查时，在 AstrBot“数据与日志”中筛选 **`Native 登录诊断`**。
更新插件并重载后，点击一次“重新获取二维码”，收集该次操作的日志：

| 日志字段 | 含义 |
| --- | --- |
| `stage=start event=request` | 已提交登录请求；随后会记录 Native 返回的初始状态 |
| `stage=status state=failed code=...` | Native 登录任务失败；`code` 为白名单内的服务错误码 |
| `stage=start/status/qr http=... code=...` | 对应 HTTP 请求失败；区分创建任务、查询状态和获取图片 |
| `stage=qr reason=invalid_content_type/invalid_signature/invalid_png/size_limit` | 图片响应类型、签名、完整性或大小校验失败 |
| `stage=restore state=failed code=not_recorded cached=true` | 恢复了旧的失败记录；点击手动重试后可采集新任务错误 |
| `stage=start event=request retry=True force_qr=True` | 手动重新扫码的新任务；不会先走旧会话 `resuming` 路径 |
| `stage=admin_token reason=unreadable_or_invalid` | 既有 Admin Token 文件无法读取或内容无效 |

日志仅记录阶段、状态、安全错误码、QR 代次和字节数，不记录 Token、二维码、请求 URL、
账号标识或 Native 原始错误正文。未知错误码显示为 `unknown`；Native 只返回 `login_failed`
时，还需结合 Native 服务自身日志定位。状态不变的轮询不会反复输出状态日志。
手动重新扫码仍复用活跃或结果不确定的原任务；策略随幂等 Key 保存，超时或重启后保持一致。

| 表现 | 检查项 |
| --- | --- |
| 普通连接检测可用，登录被拒绝 | Dashboard 登录账号是否匹配配置的管理账号；Admin Token 文件挂载、内容及账号权限是否正确 |
| 面板显示已连接但微信离线，无二维码 | 已认证账号正在重连；等待 Native 自恢复，只有未认证账号显示扫码入口 |
| 二维码过期或代次冲突 | 查询原任务，手动点击“重新获取二维码”；活跃任务会复用，不会取消未知任务 |
| “登录存储不可用” | 检查 AstrBot Plugin KV 可写性；不要清空登录 Key 后盲目重试 |
| 私聊不能绑定 | 使用 AstrBot 已认证管理员身份，确认当前平台实例存在并启用，不能在群聊操作 |
| 测试通知尝试失败 | 检查目标实例是否仍启用且类型未改变，及平台主动发送权限、配额和上下文有效期 |
| 发送调用完成但没有收到 | 在实际平台核实送达；成功返回仅代表适配器调用完成，不代表接收或已读 |

Dashboard 授权兼容性基于开发依赖固定的 AstrBot 提交
`f61a7f42696da6df25dc7a249ec9b8d54ed9da18`：其 `AuthService.login()` 认证唯一配置的
`dashboard.username`，Plugin Web API 从认证路由取得 `PluginRequest.username`。
插件严格匹配二者；配置缺失、非匹配用户或无认证信息均拒绝登录 API。
升级为多角色 Dashboard 后，应先核对宿主授权接口并更新契约测试。

## Native v1.5.1 工具操作与熔断

工具返回 `not_available` 时，先查看同一时刻的插件日志。JSON 中的友好状态不会包含内部错误细节，
插件日志会区分以下原因，且不记录查询词、聊天正文、Token 或原始响应体：

| 日志字段 | 含义与检查项 |
|---|---|
| `reason=history_tools_disabled` / `wechat_actions_disabled` | 运行中的机器人未开启对应功能；核对是否修改了当前机器人并保存应用 |
| `reason=event_type_mismatch` / `adapter_type_mismatch` | 事件或适配器类型校验失败；日志会给出事件类型，检查插件重载及事件转发路径 |
| `reason=native_client_closed` / `adapter_stopped` | 运行实例未连接或已停止，检查机器人生命周期与连接状态 |
| `reason=loop_cooldown` | 当前聊天处于循环保护冷却期 |
| `http=... code=...` | 已请求 Native；核对安全错误码、账号授权及实际 Native 版本是否支持该接口 |
| `reason=member_page_invalid` / `member_identity_missing` / `history_page_invalid` | Native 返回的分页或成员字段不符合预期，需要核对 Native 版本与响应契约 |

机器人启动时还会打印 `history_tools`、`wechat_actions` 与 `admin_token_loaded` 三个运行时布尔值。
这些值来自机器人实例启动时读取的参数。保存配置后，确认当前机器人的启动日志已显示新值；
升级或重载插件后若仍有事件类型不一致，完整重启 AstrBot，使插件模块与机器人实例重新加载。
其中 `admin_token_loaded=True` 仅表示管理凭据已读取，不能替代 Native scope 与微信实际权限校验。
工具失败后的模型回复不能当作查询结果；必须以成功的工具返回数据核对历史或群成员。

新增工具沿用同一个 SQLite 状态库。`action_intents` 保存触发消息、账号/聊天、操作类型、
参数哈希、稳定 Key、精确目标和内部 Operation ID，不保存昵称/群名正文或 Token。
`cooldowns` 保存按账号/聊天隔离的熔断截止时间；内存轨迹最多 1024 个聊天，
每个聊天最多 64 条指纹事件和 8 条近期响应计数，锁数量固定有界。

操作受理（HTTP 202）不代表完成。插件核验 Operation 的账号、kind、目标、最终 state 与
remoteOutcome/profileProjection 后才解释结果；Native 服务端接受不代表对方设备已同步。
组管理结果的本地 profileProjection 可以仍在追赶远端，工具不会冒充读取到了刷新后的资料。
普通权限不足返回业务状态，不终止 Adapter；用户只看到友好说明，内部标识和凭据不进入工具结果。

群管理若丢失首次响应，用原 Key 查询 group-mutations；有 Operation ID 时只读取对应操作。
撤回使用原始字符串 serverId（支持完整 uint64 范围）；v1.5.1 没有撤回的按 Key 查询接口，
若首次响应丢失且没有 Operation ID，只能保留 needs_review 等待人工核查，不猜测接口或重发 POST。
启动后对最多 100 条待定意图执行只读恢复；仍不确定的记录继续保留。后续同群同类请求遇到未决的
群管理操作，会先查询原操作。人工核查请结合原 Key/Operation ID 和 Native 账号对应操作记录，
不要通过清空状态库来“重试”。

修改机器人群昵称与修改整个群名授权不同：前者允许当前已启用群成员，后者与撤回仅允许
wechat_action_admins 中的真实 senderId。群回复 all/probability 模式不会扩大管理员白名单。
群管理使用专用 admin_token_file，原有凭据保持 read/send 最小权限。空管理员列表不代表允许所有人。

熔断对当前聊天静默 10 分钟，重启保留剩余时间；入站仍消费游标，但不交给模型，
发送前还会复查。撤回通知继续处理，不能被媒体发送门控挡住。
60 秒 8 次自动响应的硬限额可能误拦正常高频聊天。没有 AI 关闭熔断、退出群或全账号停机操作。

发布前运行 pytest -q、ruff check . 和现有格式检查。模拟通过不等于微信实机验证通过；
真实改名/撤回只能另行在明确授权的测试群和测试消息中验证，不自动部署生产账号。

## 消息可靠性及状态

插件自动在 AstrBot 的 `data/plugin_data/astrbot_plugin_wechat_native/` 下创建 SQLite 状态库，
按服务地址、账号及机器人实例 ID 分离。不需要手工创建文件。
请持久挂载 AstrBot 的 `data` 目录并保护其权限。运行中备份数据库需使用 SQLite backup，
不要只复制主文件而忽略 WAL。

- SSE 订阅 `/events/stream`，以保存的十进制字符串游标通过 `after` 和 `Last-Event-ID` 恢复。
- 每次连接核对 `/v1/status.serverId`，Native 数据目录被重建或同地址更换服务时停止并保留原状态，不能静默沿用旧游标。
- `message.committed` 后读取 `/messages/{messageId}`，核实账号、聊天和消息标识，再交给 AstrBot。
- 只接受 `origin == "live"` 且消息时间在当前时间前 **5 分钟内**（允许 60 秒未来时钟误差）的文本。
  历史导入、未知来源、过旧积压、非文本和撤回消息不触发 AI。请同步两台机器的时钟。
  实时消息因时间异常被忽略时，每个机器人最多每分钟警告一次，仅记录时间差，不输出正文和身份。
- 相同本地消息 ID 的 threadId/serverId 若发生变化，会明确停止并保留 `needs_review` 供人工检查，首版不自动接纳复用标识。
- 消息 ID 去重和游标在同一 SQLite 事务提交。重复/忽略消息也会推进游标。
- 撤回事件会停止仍持有的 AstrBot 事件；发送前重新读取原消息，发现撤回则不再回复。
  已在生成中的模型调用/外部工具操作不能回滚，发送前检查与 Native 接受发送之间也无法完全消除竞态。
- 每个输出先持久记录唯一 `Idempotency-Key` 再提交 **一次** POST。
  超时/5xx 后仅按原 key 查询回执，不重复 POST；`queued/submitting` 有限轮询，
  `accepted` 才标记成功。它只表示微信服务端接受，不证明对方设备已收到。
  回执 GET 遇到临时网络故障、408/429 或 5xx 时，在原截止时间内继续查询同一 send_id。
  客户端尚未连接时会在本地拒绝发送，不创建未知回执；429 后查无回执仍保留不确定状态。
- `needs_review`、失败、取消、回执不匹配、无法查询结果都不会自动重发。
  一条输入的任一片段发送失败后，后续发送会停止，避免在同一响应中制造重复或部分续发。

**队列消费与失败边界：** AstrBot 内部队列没有跨插件的事务确认接口，因此不能承诺端到端 exactly-once。
消息交付队列后记为 `queued`（已交给 AstrBot，尚未确认回复）；回复成功后记为 `replied`。
明确调用发送但输出为空时记为 `no_reply`，重启不会转为待核查；之后仍允许该事件产生非空回复。
若该事件随后开始真实发送，会与发送记录在同一事务中恢复为 `queued`，避免崩溃后误显示无需回复。
发送失败保留 `failed` 或 `needs_review`；已有片段成功、后续片段失败或未确认时记为 `partial`，
每段原始回执仍保留在 sends 表，后续自动续发会被阻止。撤回为 `recalled`；过滤为 `ignored`。
重启时会结合未确认的发送记录恢复 `partial`；已经出现失败或取消后，即使在途片段的成功回执
稍后到达，也不会解除后续发送门控。`replied` 表示已有片段确认成功，不代表流水线已全部完成。
重启时尚为 `queued` 的记录转为 `needs_review`，**不会重新交给模型或重发**。
这也包括正常流水线选择不回复、模型失败、进程在交付窗口崩溃等情况，需结合 AstrBot 日志判断。
游标提交失败前的事件会重放；已交付但未确认回复的消息不会悄悄被视为成功。

状态库只存消息/聊天/服务端标识、游标、幂等键与回执状态，**不存聊天正文和 Token**。
这些标识仍属私人数据，请勿公开状态库。首版不自动删除去重记录，磁盘占用随消息量增加。
本插件日志不输出聊天正文或 Token；AstrBot 核心/其他插件自己的会话日志不受本插件控制。

开启群聊摘要后，符合机器人群策略的实时文本会额外存入有上限的内存缓存，不写入上述状态库。
重启/停止即清空，过期记录在下一次收集或读取时清理；生成摘要时仅把当前群选中的记录交给会话模型。
模型失败或超时可重新发送摘要命令；如果已经出现未确认发送回执，仍按下面的回执流程排查。

### 排查未确认回复（只读）

在 AstrBot 宿主机本地检查状态库，不要上传：

```bash
sqlite3 -readonly /path/to/state.sqlite3 \
  'SELECT id,status FROM messages WHERE status IN ("queued","needs_review","failed","partial");'
sqlite3 -readonly /path/to/state.sqlite3 \
  'SELECT key,state,send_id FROM sends WHERE state != "accepted";'
```

有 send_id 时读取 Native `GET /v1/accounts/{accountId}/sends/{sendId}`；
也可用 `GET /v1/accounts/{accountId}/sends/by-idempotency-key?key=原键` 查询。
使用安全的本地凭据方式请求，勿把 Token 放进聊天、脚本仓库或终端录屏。
确认 Native 回执及真实聊天后再由人决定是否补发；不要删除状态库或换 key 来自动“修复”。
插件不提供自动重试不确定业务动作的按钮。

### 新加入的群回复时返回 404

群回复首段会自动 @原发送者。Native 在发送带 @的文本前，需要从已保存的群成员资料中
确认机器人和被 @成员；新群资料尚未保存时，可能返回 `404 media_not_cached`，
成员资料不完整时也可能返回 `409 mention_member_unavailable` 或
`409 mention_group_membership_unavailable`。旧版插件把这些错误统一显示为 `request_failed`。

插件现在会在发送带 @的群回复前检查所需成员，缺失时调用
`POST /groups/{groupId}/members/refresh`，等待刷新完成并重新检查成员，再核对原消息是否撤回。
从 v0.2.3 起，刷新失败、超时或所需成员仍未保存时，插件会省略首段自动添加的 @，
继续发送回复正文；这发生在创建发送回执之前，仍只提交一次发送。消息链中的显式 @
仍要求成员资料可用，失败时停止发送；登录失效、账号或回执身份不匹配也不会降级。
资料已存在时不会重复刷新。刷新不可用的日志仅记录安全的操作状态、错误码及操作 UUID，
不输出 Native 回执正文；可按操作 ID 查询 `GET /operations/{operationId}` 继续排查。
此刷新只需原有的 `read` 权限，不需要群管理权限。

升级插件后，重启整个 AstrBot 进程或容器（本文 Docker 部署可执行 `docker restart astrbot`），
再在新群重新 @机器人发送一条新消息验证。仅更新插件文件或热重载插件，已有的机器人实例
可能仍运行旧代码；堆栈中的函数名与显示的源码行不匹配，是运行代码与磁盘文件不同步的线索。
已经标记为 `failed` / `needs_review` / `partial` 的旧回复不会自动补发，仍按上面的回执流程核查。
