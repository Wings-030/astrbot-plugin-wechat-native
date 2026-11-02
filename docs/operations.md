# 运维说明

[返回 README](../README.md)

本页说明消息可靠性、状态保留和回执排查。安装、配置及 Docker 部署请参阅 README。

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
