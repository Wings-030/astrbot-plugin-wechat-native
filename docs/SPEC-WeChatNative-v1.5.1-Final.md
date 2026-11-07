# SPEC — AstrBot WeChat Native v1.5.1：群聊互动、历史查询、微信操作与 Loop Breaker

- **版本**：1.0（最终需求基线）
- **目标仓库**：[neozhu/astrbot-plugin-wechat-native](https://github.com/neozhu/astrbot-plugin-wechat-native)
- **目标依赖**：[thisnick/agent-wechat `v1.5.1`](https://github.com/thisnick/agent-wechat/releases/tag/v1.5.1)
- **范围**：7 个 AstrBot LLM Function Tools + 1 个 Adapter 内置 Loop Breaker
- **基本原则**：现有插件内集成；Native `/v1` API 为唯一微信执行通道；无独立插件、服务、RAG、向量库或新数据库；不破坏原有 SSE、媒体、出站回执和去重能力。

## 1. 目标与非目标

### 1.1 目标

让微信人格助手「阿拉蕾」在自然语言对话中：

1. 检索当前私聊/群聊的 Native 本地消息历史。
2. 阅读最近消息，回答“刚才在聊什么”等问题。
3. 读取当前群资料和成员名单。
4. 修改**机器人自己**在当前群的昵称，以及在微信权限允许时修改**整个群名称**。
5. 撤回当前会话中机器人自己发送、且被 Native 判定可撤回的消息。
6. 无需二次确认即可执行已授权的写操作；遇到权限不足自然解释，不抛出技术错误给用户。
7. 不论对方是谁，一旦识别出反复互相回复造成的对话循环，立刻静默熔断当前会话的自动回复。

### 1.2 非目标

- 不允许任意跨群/跨账号搜索；不为普通用户开放整个账号历史和联系人目录。
- 不修改其他群成员昵称，不移除群成员，不退出群，不修改群公告。
- 不发送图片/视频/文件或接入文生图模型（现有图片发送应保持可用；新能力另开 SPEC）。
- 不实现 AI 自行禁用熔断、基于 LLM 的机器人身份识别或调用外部分类模型。
- 不承诺 Native 本地历史覆盖全部远程微信历史。
- 不把操作失败伪装成成功，也不让 LLM 自主生成/校验微信权限。

## 2. 当前项目及兼容性事实

当前仓库采用 `main.py`（Star 与工具/指令入口）、`adapter.py`（Native 平台适配器、SSE 和出站）、`event.py`（`WeChatNativeEvent`、可信 `raw_message` 与 `session_target()`）、`native_client.py`（复用的 `aiohttp` HTTP 客户端）、`state.py`（现有 SQLite 状态库）、`config.py`（配置）、`summary.py`（近期群摘要内存缓存）。

- 账号：取 `event.adapter.native_config.account_id`，而不是模型输入。
- 聊天：从同一个可信 `event.message_obj.raw_message['threadId']` 获取并与 `session_target()`、账号及消息身份交叉检查。
- 实际发送者：`raw_message['senderId']`；禁止通过显示昵称、文本中的 `@` 或 LLM 推断授权身份。
- 群聊：`threadId` 以 `@chatroom` 结尾；群内真实 @ 判定优先使用 `mentions.state == 'known' && mentions.self is True`。
- `unique_session` 仅影响 AstrBot 的上下文划分，绝不能扩大工具可访问的微信聊天范围。
- Native v1.5.1 `/v1` 路由及 `NativePage` 的 `items`、`nextCursor` 等字段保持为本文约定的依据。
- 不能用 `summary_cache` 代替 Native 历史查询；前者是短期内存快照、重启即清除。

## 3. 最终 Tool 清单（严格 7 个）

| 编号 | 注册名 | 范围 | 操作 | 是否需二次确认 |
|---|---|---|---|---|
| T1 | `wechat_search_messages` | 当前群/私聊 | 搜索已保存消息 | 否 |
| T2 | `wechat_get_recent_messages` | 当前群/私聊 | 读取最近消息 | 否 |
| T3 | `wechat_get_group_info` | 当前群 | 查询群资料 | 否 |
| T4 | `wechat_list_group_members` | 当前群 | 列出/查找成员 | 否 |
| T5 | `wechat_set_my_group_nickname` | 当前群 | 修改机器人自己的群昵称 | 否 |
| T6 | `wechat_rename_group` | 当前群 | 修改微信群名称 | 否 |
| T7 | `wechat_recall_own_message` | 当前群/私聊 | 撤回机器人自己的消息 | 否 |

**不注册** `loop_breaker`、`wechat_detect_bot` 或 `wechat_exit_chat` 为 Tool：Loop Breaker 必须在 Adapter 内自动运行。

### 3.1 AstrBot 公开工具方法签名

> 仅为接口契约示例。实际实现在完成认证/环境校验后通过内部 service 调用 `NativeClient`；请依据当前 AstrBot v4 SDK 约定编写真实 Docstring。不要在 Tool 内调用 `event.send()`，统一返回 JSON 字符串。

```python
@filter.llm_tool(name="wechat_search_messages")
async def wechat_search_messages(self, event: AstrMessageEvent, query: str, limit: int = 20, cursor: str = "") -> str: ...

@filter.llm_tool(name="wechat_get_recent_messages")
async def wechat_get_recent_messages(self, event: AstrMessageEvent, limit: int = 20, cursor: str = "") -> str: ...

@filter.llm_tool(name="wechat_get_group_info")
async def wechat_get_group_info(self, event: AstrMessageEvent) -> str: ...

@filter.llm_tool(name="wechat_list_group_members")
async def wechat_list_group_members(self, event: AstrMessageEvent, query: str = "", limit: int = 20, cursor: str = "") -> str: ...

@filter.llm_tool(name="wechat_set_my_group_nickname")
async def wechat_set_my_group_nickname(self, event: AstrMessageEvent, nickname: str) -> str: ...

@filter.llm_tool(name="wechat_rename_group")
async def wechat_rename_group(self, event: AstrMessageEvent, name: str) -> str: ...

@filter.llm_tool(name="wechat_recall_own_message")
async def wechat_recall_own_message(self, event: AstrMessageEvent, message_id: str = "") -> str: ...
```

不向任何 Tool 暴露 `account_id`、`thread_id`、`group_id`、URL、Token、Idempotency-Key 或 Native operation ID 作为可由 LLM 修改的目标选择参数。`message_id` 只用于当前聊天内的精确定位，不能突破会话边界。

## 4. Native API 对照（v1.5.1）

以下路径均以 `/v1/accounts/{accountId}` 为前缀；其中 `{accountId}` 来自可信配置。

| Tool | 路由 | Native Token Scope / 注意事项 |
|---|---|---|
| T1 | `GET /threads/{threadId}/messages?query=...&limit=...&cursor=...` | `read`；本地子串搜索 |
| T2 | `GET /threads/{threadId}/messages?limit=...&cursor=...` | `read`；从新到旧 |
| T3 | `GET /groups/{groupId}` | `read`；保存的群元数据可能过期 |
| T4 | `GET /groups/{groupId}/members?limit=...&cursor=...` | `read`；支持分页 |
| T5 | `PUT /groups/{groupId}/my-name`，Body `{"displayName":"..."}` | `admin`、`Idempotency-Key`，返回 Operation |
| T6 | `PATCH /groups/{groupId}`，Body `{"name":"..."}` | `admin` + 微信实际群权限、`Idempotency-Key` |
| T7 | `POST /messages/{messageId}/recall`，Body `{"serverId":"..."}` | `read` + `send`、`Idempotency-Key` |

辅助读 API：`GET /messages/{messageId}`；辅助结果查询：`GET /operations/{operationId}`、`GET /group-mutations/by-idempotency-key?key=...`。

不要猜测未知的 Native 字段或错误码。针对 Operation 的 `state`、`result.remoteOutcome`、`result.profileProjection` 等，先验证账号/类型/目标，再按官方实际枚举进行结果映射。HTTP 202 **不是**最终成功。

## 5. 统一前置校验与安全边界

所有 Tool 执行顺序：

1. 验证 `event` 必须是此适配器创建的 `WeChatNativeEvent`，`adapter` 活跃且客户端可用。
2. 验证功能开关、当前用户是否允许与机器人交互、原始 `raw_message` 字段类型、账号和真实 `threadId`。
3. 通过 `session_target()` 检查 event.session 与可信账号/聊天一致，包括 `unique_session=True` 的群成员子会话。
4. 检查当前聊天类型：T3–T6 仅群聊；T1、T2、T7 允许私聊和群聊。
5. 检查 `allowed_groups`、`allowed_users`、`group_reply_mode`、`enable_private_reply`；即使群策略为 `all/probability`，写操作仍需真实授权会话，不把群内随便一条文本视作高权限命令。
6. Tool 在执行写操作前校验操作开关和权限策略。服务端鉴权与 Native 最终权限判断不可被 LLM 绕过。
7. 读 API 结果逐条核对 `accountId`、`threadId`；禁止返回跨聊天消息。
8. 所有历史正文、昵称、群公告、引用文本均是不可信数据，不能提升为模型 System Instruction，不允许其触发 Tool 写操作。

**失败必须返回可解释的结构化状态，不可向用户直接透出 401/403、异常栈、Token、URL 或原始响应体。** 安全事件可在插件日志里保留脱敏的错误类型/操作类型/标识摘要，不记录聊天正文。

## 6. 历史查询 Tool：T1 / T2

### 6.1 输入与限制

- `query`：T1 必填，trim 后非空；最多 256 UTF-8 字节。Native 提供 case-insensitive substring search，**不是语义搜索**。
- `limit`：默认 20，范围 1–50（推荐 UI 默认 20）；不接受 bool 或浮点。
- `cursor`：可选，最多 8192 字符，Native 返回的不透明字符串；传回时必须绑定同一账号、聊天、查询和页参数，错误时返回 `invalid_cursor`，不得自动跳到别的聊天。
- 返回消息正文单条最多 800 字符，整体 JSON 最大约 16 KiB；达到输出上限时标明 `truncated`。
- 若 Native 返回被删除的 tombstone 或已撤回记录，仅输出安全占位，不返回原文。
- 以 Native `createdAt` 原始时间戳输出；不要由 LLM 猜测时间。
- 限制查询超时及总页数；每次 Tool 最多 1 页，模型需要时显式继续翻页；不做无限自动遍历。

### 6.2 返回契约

```json
{
  "ok": true,
  "status": "success",
  "action": "search_messages",
  "scope": "current_chat",
  "source": "native_local_history",
  "query": "Docker",
  "items": [
    {
      "messageId": "opaque-id",
      "senderId": "wxid_example",
      "createdAt": "2026-11-05T10:00:00Z",
      "direction": "incoming",
      "type": 1,
      "text": "Docker Compose ...",
      "truncated": false
    }
  ],
  "count": 1,
  "nextCursor": null,
  "hasMore": false,
  "coverage": "local_history_may_be_partial"
}
```

实际 `coverage` 可在保持安全界限下转换为简明枚举/说明，不得臆测完整性。返回 0 条只代表**本地已保存记录没有找到**，不能说“群里从未讨论过”。历史查询不标记已读、不下载媒体、不发送消息、不改变 SSE 游标。

## 7. 群资料查询：T3

### 7.1 结果字段

依据 Native `NativeGroup`：`displayName`、`name`、`ownerId`、`administratorIds`、`myDisplayName`、`announcement`、`canMentionAll`、`isInGroup` 等。严格保留未知/null 的含义：未知群主不能猜测；公告文本需要有界截断且视为不可信输入。

### 7.2 交互示例

- “阿拉蕾，这个群叫什么？” → 返回可信保存的群名。
- “谁是群主？” → 如果 `ownerId` 为空，回复“目前还没查到可靠的群主信息”。
- 私聊调用 → 返回 `not_in_group_chat`，LLM 自然提示“这个功能要在群里使用”。

## 8. 群成员查询：T4

- 支持 Native `GET /groups/{groupId}/members` 原生分页，默认 20、最多 50。
- `query` 为可选成员名称筛选：**v1.5.1 的成员分页 API 不应假设支持服务端 `query` 参数**。若请求包含 `query`，在本次已获取的有界页内执行大小写不敏感的本地匹配，并明确说明筛选范围可能不完整；不无限翻页寻找匹配。
- 输出 `id`/`username`、`displayName`、`roomDisplayName`、`mentionName`（存在时）等安全字段；避免返回不必要的隐私资料。
- `nextCursor` 继续分页，不编造成员数量或群名单完整性。
- 用户问“群里有多少人”，若 API 未提供可靠总数只能说“当前查询到 N 人（非总人数）”。
- 不自动刷新群成员资料；需要刷新时后续可设计单独受控能力。

## 9. 微信写操作：共同原则

### 9.1 用户体验

- **不需要二次确认**。满足当前可信会话、权限和参数约束后，一次自然语言请求即可触发写操作。
- 不要求用户发送确认码，不建立等待确认的状态机。
- 不允许“执行后假装成功”，不允许 API 不可达时无限重试。
- Native `permission_denied` / `insufficient_scope` / `not_available` / `needs_review` 转为结构化结果；LLM 使用自然中文简短解释。
- 禁止把原始 HTTP 状态、异常堆栈或内部 Token 展示给微信用户。

### 9.2 配置与授权

- 读 Tool 默认关闭，通过 `enable_history_tools` 开启；群信息读取与历史读取共用这个读能力开关。
- 写 Tool 默认关闭，通过 `enable_wechat_actions` 开启。
- `wechat_action_admins` 为可信 `senderId` 白名单（默认空）。**撤回**仅允许该列表中的账号；建议**修改整个群名**也使用该白名单作为插件侧授权；修改机器人自身群昵称可允许当前已启用群中的成员发起（受限流约束）。
- 若希望群成员都能改群名，必须由管理员明确改变插件侧授权策略；不能仅靠 Native `admin` Token 推断“任何群成员都能代表机器人执行群管理”。本 SPEC 默认不开放给普通群成员。
- 凭据遵循最小权限：现有 read/send Token 不必自动扩大为 admin；可以新增可选 `admin_token_file` 与专用 NativeClient 写入实例（仅在写功能启用时初始化）。若暂不实现分离，必须明确安全限制及部署条件，不允许静默提权。
- 普通权限拒绝属于业务状态，不能造成 Adapter/Agent 生命周期异常。

### 9.3 幂等与回执

- 将每次写操作绑定可信触发消息 ID、账号、Thread、操作类型、目标和参数哈希，生成稳定 `Idempotency-Key`。
- 在**首次 HTTP POST/PATCH/PUT 前**，先持久化写操作意图、Key、目标摘要到现有 SQLite；不得存入 Token。
- 每个操作只提交一次原始 mutation 请求；超时后只用相同 Key 查询既有操作结果，绝不能自动换新 Key 重发。
- 对 202 Operation，检查最终 `state`、Native result 中可用的 `remoteOutcome`/`profileProjection`；仅在有最终成功证据时返回 `success`。
- `pending`/`needs_review` 必须明确，保留 operation ID（内部）与定位所需的持久化记录。
- 单条用户消息反复触发同一 Tool，返回既有操作状态，不再次修改。
- 修改类操作建议按 `(account, thread, action)` 序列化，避免并发改名互相覆盖。

## 10. 修改机器人自身群昵称：T5

- 路由：`PUT /groups/{groupId}/my-name`。
- Payload：`{"displayName": nickname}`。
- 输入昵称非空、trim、限制合理字符/UTF-8 字节数（以 Native 校验约束为最终依据）、拒绝控制字符；不要截断后悄悄改成另一个名字。
- 只能操作当前群中的机器人自己；不允许指定其他人的 `senderId`。
- 当前群成员可通过自然语言发起，无需二次确认，但同群相同操作应限流，例如每群 60 秒最多修改一次。
- 输出成功示例：“好耶，以后我在这个群就叫「摸鱼小助手」啦～”；权限不足则回答“哎呀，当前还没法修改我的群昵称，等权限准备好再试试～”。

## 11. 修改整个微信群名：T6

- 路由：`PATCH /groups/{groupId}`。
- Payload：`{"name": name}`。
- 仅当前群；输入长度和字符验证同 T5，但按 Native 的群名校验要求执行。
- **默认仅 `wechat_action_admins` 可以发起**，且需要 Native admin scope 及微信账号在目标群的实际管理权限。
- 正常权限不足返回 `permission_denied`；Token 授权不足返回 `not_available`/`configuration_error`，不能误称微信群主拒绝。
- 接口返回 Operation 后检查远端结果，确定成功才回复“群名已经改成……”。
- 如果当前群状态不存在/资料未准备，返回可理解的 `not_available`，不直接抛出异常给用户。

## 12. 撤回机器人自己的消息：T7

- 路由：`POST /messages/{messageId}/recall`。
- Payload：`{"serverId": "<精确存储的字符串>"}`。
- **默认仅 `wechat_action_admins` 可发起**；无需二次确认。
- 只接受当前可信会话的目标消息；先 `GET /messages/{id}` 或从当前 Native 保存的历史中精确检索，再验证 `accountId`、`threadId`、`direction='outgoing'`、账号自身 `senderId`（如存在）、未撤回、`serverId` 非空且格式合法。
- `serverId` 必须是原始字符串，绝不能转换浮点/JS Number，避免超出安全整数范围。
- `message_id` 为空表示自然语言“撤回刚才那条”之类的目标解析：仅查询**当前聊天**最近消息并寻找机器人自己发出的最近一条；如果唯一性或具体所指不清晰，返回 `ambiguous_target`，让 LLM 简短追问，不自行猜测。
- 禁止使用 `sendId`、`jobId`、`operationId` 替代 Native 原始消息 ID。
- 已撤回的消息返回 `already_recalled`（不再次 POST）；已超出微信允许的撤回窗口，返回 `not_recallable`。
- 处理 Native 异步回执；遇到身份变化、ID 复用、状态不确定，立即停止并保留人工核查信息。

## 13. 标准化 Tool 输出与友好错误

统一返回 UTF-8 JSON 字符串：

```json
{
  "ok": false,
  "status": "permission_denied",
  "action": "rename_group",
  "message": "我现在没有权限修改这个群的名称",
  "retryable": false
}
```

状态枚举：`success`、`permission_denied`、`not_available`、`configuration_error`、`invalid_request`、`not_in_group_chat`、`ambiguous_target`、`invalid_cursor`、`not_recallable`、`already_recalled`、`rate_limited`、`pending`、`needs_review`、`failed`。

- `permission_denied`：已知微信侧或插件侧操作权限拒绝。
- `configuration_error`：Token scope/config 不能满足工具调用，内部记录必要配置排查线索，群友只见友好说明。
- `pending`：Native 已接受、仍在执行；不能告诉用户“已经改好”。
- `needs_review`：超时/回执不确定；返回“操作结果暂时无法确认”，不要再次尝试自动提交。
- 模型不得因 Tool 错误而循环重试；同一触发消息最多一次写请求。
- Tool 内捕获业务可预期错误并转换；程序 bug/意外错误记录脱敏日志，仍以通用安全响应面向微信用户。

## 14. P0：Loop Breaker（非 Tool）

### 14.1 目标

**与对方身份无关**：不检测、拉黑或判断对方是否“智能客服”“机器人”或真人。只检查当前微信会话是否正在形成自动回复闭环。发现循环即**静默停止**：不给对方发“暂停回复/退出/已熔断”之类的提示，避免这条提示再次触发对方自动回复。

“退出”仅指停止这个私聊或群聊的自动响应，不调用退出群 API、不注销微信账号、不停其他会话。

### 14.2 监控数据

每个 `(accountId, threadId)` 独立维护最近 60 秒的有界轻量轨迹，包含：

- 入站/出站方向、可信 message ID 与时间戳。
- 规范化文本指纹（Unicode 规范化、去多余空白、可选择忽略开头的真实 @ 标签）。
- 发送结果是否属于本 Adapter 自动回复，是否已经被 Native 接受。
- 原始触发消息与出站回复的对应关系（尽可能从现有 StateStore 回执关联获取）。

不把原始正文写入新的持久化日志。轨迹可放内存；仅保存熔断截止时间及必要状态到现有 SQLite。需要限制每个会话条数和总会话数量，防止资源无限增长。

### 14.3 判定策略

只有在**真实入站与本机器人出站交替**的条件下才判为自动回复循环，避免把群里多个真人单纯刷屏当成循环。

- **内容循环**：同样的规范化文本/高相似文本在连续 A→B→A→B 往返中多次重复（例如 3 轮），触发熔断。
- **时间循环**：60 秒内出现至少 3 组很短间隔的双向自动往返，且回复内容出现显著重复或模式相似，触发熔断。
- **硬兜底**：同一会话 60 秒内产生 8 次由机器人自动发送的响应时进入熔断。此条件有正常高频聊天误报风险，必须在 SPEC/README 明确，且绝不能影响其他 Thread。
- 所有模式匹配必须有界，禁止额外调用 LLM 进行检测。
- 同一会话一旦命中，熔断 10 分钟，并清理该会话在内存中的循环轨迹。
- 冷却结束自动恢复；重启后仍保留剩余冷却时间。

**关键执行边界**：

1. `process_native_event()` 在 `state.accept()`/向 AstrBot `commit_event()` 前检查熔断，拒绝的事件仍应以安全方式推进 SSE 去重/游标状态，但不交给模型。
2. `send_chain()` 在任何实际 Native POST 前再次检查熔断。已有生成中回复不能绕过熔断。
3. 所有自动回复路径都受保护：文本、媒体错误提示、语音/图片回复、命令、群聊摘要；必要的明确管理员外发动作是否豁免须在服务层审慎区分，不允许普通 LLM Tool 绕过。
4. 同一个 Thread 的并发消息与发送必须用同一线程级互斥/串行门控，以免两个任务同时判断未超限而发送。
5. 熔断触发后**不发送任何提示消息**，不得用 `event.plain_result()` 产生可见自动回答。
6. 熔断与现有消息 `recalled`、`needs_review`、`partial` 状态不可互相覆盖；被拦截的输入应标记合理的 `ignored` / `no_reply`，保留原有 exactly-once 限制。
7. 日志只记 account/thread 的不可逆短摘要、触发规则、时间区间，不记正文与凭据。

### 14.4 对外行为

- 正常聊天：保持原有唤醒逻辑，不额外触发工具。
- 识别循环：当前会话立即静默，10 分钟后正常工作。
- 其他群、其他私聊和其他账号：完全不受影响。
- 无法确定是否循环：保守执行；但硬限额保证最坏情况有上界。

## 15. 建议的源代码改动范围

| 文件 | 变更 |
|---|---|
| `main.py` | 注册 7 个 LLM Tool，并接入受控服务方法；保持现有 PDF、群摘要入口 |
| `config.py` | 新增最多 3 个基础配置 + 可选管理员凭据文件；校验默认值 |
| `native_client.py` | 添加分页查询、群资料、群成员、群改名、自己群昵称、撤回 API；复用现有连接、异常脱敏与关闭逻辑 |
| `event.py` | 不改 Session 公开契约；仅在需要时增加可信事件上下文提取 helper |
| `adapter.py` | 集成 Loop Breaker 的入站/出站门控及生命周期管理 |
| `state.py` | 为写操作意图/Operation 和熔断截止时间新增**同一个 SQLite** 内的小型表/字段，不存正文或 Token |
| `history_tools.py` | 新增内部模块：T1–T4 读服务、分页与结果安全整理 |
| `wechat_actions.py` | 新增内部模块：T5–T7 参数校验、权限检查、幂等和结果转换 |
| `loop_guard.py` | 新增内部模块：有界模式检测、熔断状态与并发保护 |
| `tests/` | 新增读写与循环测试；现有测试不删减 |
| `README.md` / `docs/operations.md` / `metadata.yaml` | 文档和 Native v1.5.1 兼容说明；不夸大实际验证程度 |

以上 `*.py` 新增文件只是**同一插件里的普通模块**，不是新插件或额外服务。

### 15.1 最小配置建议

```python
# Existing platform config, proposed additions
"enable_history_tools": False,
"enable_wechat_actions": False,
"wechat_action_admins": [],
# Optional, only if using separate admin-scope credential:
"admin_token_file": "",
```

- `Loop Breaker` 默认开启：60 秒检测窗、10 分钟熔断、8 次兜底，首版用常量即可；避免膨胀配置界面。
- 保持原有 `allowed_users`、`allowed_groups`、`enable_private_reply`、`group_reply_mode`、`unique_session` 不变。
- 如需管理员操作权限，读取 `wechat_action_admins` 的真实 `senderId`；空列表不等于所有人可执行高权限管理操作。
- T5（机器人自己昵称）与 T6/T7 分开授权：T5 可以向允许的群成员开放；T6/T7 默认管理员专用。
- 管理员 Token 优先采用已有凭据文件模式，不把 Token 明文加入日志或版本库。

## 16. 必须实现的测试矩阵

### 16.1 Tool 与读操作

- 7 个 Tool 恰好注册且名字/参数匹配；无其他平台事件可以调用。
- 功能开关关闭不产生任何 Native HTTP 请求。
- 群/私聊正确、私聊调用群 Tool 被拒绝；跨账号和跨 Thread 响应拒绝。
- `unique_session=True/False` 都不会扩展历史查询范围。
- 中文搜索词编码、空 query、过长 query、非法 cursor、分页继续、空结果、不完整历史。
- Native tombstone/recall/媒体占位安全处理；禁止消息正文泄漏到日志。
- 群主未知、公告为空、群成员分页、同名成员、本地成员筛选范围不完整。

### 16.2 修改与撤回

- 群成员请求修改机器人自己昵称成功，无二次确认。
- T5 60 秒限流、非法昵称、不在群里请求、跨群拒绝。
- T6 的管理员白名单、Native admin scope、实际群管理权限，均独立覆盖。
- T6 权限不足时 Tool 返回友好状态，LLM 不看到原始异常。
- T7 只撤回本账号当前聊天的 outgoing 消息；incoming、跨群、跨账号、ID 复用、无 serverId 等拒绝。
- `serverId` 保持字符串，特别是超过 JS Number 安全整数范围的值。
- 用户说“撤回刚才那句”而多个目标有歧义时不误撤回。
- 单一用户请求只提交一次 mutation；确认无需第二轮，但状态仍需持久化。
- 202 pending、操作成功、权限拒绝、超时、未知回执、重启恢复；不能换 Key 自动重试。

### 16.3 Loop Breaker

- 重复 A/B 往返达到阈值后静默熔断；不要求识别机器人身份。
- **完全不重复但持续快速往返**达到硬上限后熔断。
- 真人普通短会话不误熔断；快速群消息但无机器人出站不熔断。
- 已进入模型队列的并发回复在最终发送前被阻断。
- 图片、语音、媒体错误提示和群摘要发送路径不能绕过。
- 熔断只影响一个 `(account, thread)`；其他群、私聊、其他账号不受影响。
- 重启保留 10 分钟冷却剩余时长；过期恢复；重复 SSE 回放不重复计数。
- 熔断时不发“我退出了”“暂停”或任何其他提示；不调用 `leaveGroup`。
- SSE Cursor、消息状态、已有发送回执与撤回事件无回归。

### 16.4 回归命令

```bash
pytest -q
ruff check .
```

必须在项目实际依赖环境运行；保留已有 `tests/test_adapter.py`、`tests/test_native_client.py`、`tests/test_summary.py`、`tests/test_send_images.py`、`tests/test_voice.py`、`tests/test_pdf.py` 和其他测试。

建议增加 Native v1.5.1 模拟 HTTP fixtures 与真实 AstrBot LLM Tool 注册契约测试。**模拟测试通过不等于微信实机验证通过**。

## 17. Definition of Done / 验收场景

1. 微信群中“阿拉蕾，刚才聊了什么？”调用 T2，返回当前群的真实本地历史摘要。
2. 微信群中“找一下上周说过的 Docker”调用 T1，若本地找不到，明确提示“已保存消息里暂未找到”。
3. “群主是谁？”调用 T3；资料缺失时不编造。
4. “看看群里有哪些人？”调用 T4，按 Native 分页返回有限名单。
5. “你以后叫摸鱼小助手”调用 T5，在权限允许时立即执行，**不二次确认**。
6. 管理员说“把群名改成快乐摸鱼总部”调用 T6，Native 确认成功后才回复已完成；权限不够则自然说明。
7. 管理员说“撤回你刚才发的消息”调用 T7，能精确定位则撤回，无法唯一确定则询问，不误撤回。
8. 任意对象与阿拉蕾持续互相触发回复，一旦循环出现，Loop Breaker 静默中断，十分钟冷却，其他聊天不受影响。
9. Native HTTP 异常、权限不足、异步操作待定都不使 AstrBot 崩溃，也不引起 Tool 无限重试。
10. 完成单元/集成/回归测试，记录实际运行的测试结果与真实微信验证状态。

## 18. 分阶段实施建议

1. **Phase 0：Loop Breaker（P0）**：先独立实现并测试，消除现有死循环稳定性问题。
2. **Phase 1：Read Tools（T1–T4）**：历史/群信息/成员，统一可信上下文与 JSON 输出。
3. **Phase 2：Write Tools（T5–T7）**：最小权限、无二次确认、异步回执与幂等。
4. **Phase 3：v1.5.1 回归与文档**：验证 SSE、媒体、历史摘要、出站回复未退化。

不要未经用户授权自动部署生产微信账号或执行真实群改名/撤回。开发可自动完成模拟测试；真实修改类验证使用明确的测试群和测试消息。

## 19. 依据与参考

- [Native v1.5.1 Release](https://github.com/thisnick/agent-wechat/releases/tag/v1.5.1)
- [Native v1.5.1 API 文档](https://github.com/thisnick/agent-wechat/blob/v1.5.1/docs/src/content/docs/use/api.mdx)
- [Native v1.5.1 群管理文档](https://github.com/thisnick/agent-wechat/blob/v1.5.1/docs/src/content/docs/use/groups.mdx)
- [Native v1.5.1 SDK 源码](https://github.com/thisnick/agent-wechat/blob/v1.5.1/packages/shared/src/native-client.ts)
- [Native v1.5.1 类型定义](https://github.com/thisnick/agent-wechat/blob/v1.5.1/packages/shared/src/native.generated.ts)
- [已有适配器源码](https://github.com/neozhu/astrbot-plugin-wechat-native)

**最终交付物**：在现有插件内实现 7 个 LLM Tools、1 个 Loop Breaker 机制，所有高权限写操作无需二次确认但必须受真实权限与可恢复幂等约束。项目保持可部署、可回归测试和可维护。
