# 运维说明

[返回 README](../README.md)

本页说明消息可靠性、状态保留和回执排查。安装、配置及 Docker 部署请参阅 README。

## Native v1.5.1 工具操作与熔断

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
