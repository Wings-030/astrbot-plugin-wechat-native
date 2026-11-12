# Development SPEC — WeChat Native QR Login & Cross-Platform Connection Alerts

> **版本：2.0（重新生成）** · **日期：2026-11-12** · **状态：Ready for Implementation**  
> **项目：** [`neozhu/astrbot-plugin-wechat-native`](https://github.com/neozhu/astrbot-plugin-wechat-native)  
> **Native 兼容基线：** `thisnick/agent-wechat` **v2.3.0**（必须以此 tag 验证）  
> **宿主：** 支持 Plugin Views、Plugin Web API、Command Alias、Plugin KV 及 `Context.send_message()` 的 AstrBot 版本  
> **建议分支：** `feat/native-qr-login-cross-platform-alerts`

## 0. 冻结决策（不得偏离）

1. **现有 WeChat Native 测试面板内扫码登录**，复用 UI，不新建后台服务、不运行 `wx` CLI、不修改 Native Daemon。
2. Native 对接版本统一为 **v2.3.0**；登录优先走 `/login-attempts` API，而非旧版本假设。
3. 登录操作复用现有 `admin_token_file`（默认 `/run/secrets/wechat-admin-token`），连接检测继续使用 `token_file`（默认 `/run/secrets/wechat-api-token`）；**不增加 `login_token_file` 或新 Docker Secret**。
4. **跨平台通知绑定完全通过聊天指令**，不暴露平台 ID、机器人 ID、Session ID、UMO 或监控开关等新增用户配置项。
5. 四组指令：**`/绑定通知`、`/解绑通知`、`/通知状态`、`/测试通知`**；英文别名分别为 `/bind_alert`、`/unbind_alert`、`/alert_status`、`/test_alert`。
6. **只有一个启用的 Native 机器人时可省略目标 ID；多个时必须显式指定**。每个 Native 机器人最多绑定一个接收会话。
7. **每次事故断线通知最多发送一次，恢复通知最多发送一次**。下一次新的断线重新发送；监控与去重状态持久化，重启不重复告警。
8. 监控在 AstrBot 后台运行，与面板打开/关闭无关；**未绑定只监控，不发送**。监控本身不执行登录操作。
9. 命令权限只允许 AstrBot **已认证的管理员在私聊中操作**。不通过昵称或消息文本推断管理员身份。
10. 消息发送是 **best effort / at-most-once attempt**：不保证平台接受后用户已读或送达；失败不自动重复发送。

---

## 1. 背景、目标与非目标

### 1.1 当前项目基线

现有插件已包含：

- `connection_view.py`：`GET connection/bots`、`GET connection/check`、`GET connection/panel`，只读检测 Native 服务与账号状态。
- `native_client.py`：`NativeClient`，使用 Bearer Token 调用 Native API。
- `views/connection/index.html`、`app.js`、`style.css`：AstrBot Plugin View 连接面板，展示连接状态、统计和图表。
- `config.py`：`token_file` 和 `admin_token_file` 两个现有 Secret 文件路径。
- `main.py`：注册 Plugin Web API，已有 Command Handler 与业务能力。
- 现有自动化测试，包括连接检测、UI、Native Client 等；本开发只能增量扩展。

### 1.2 用户目标

用户可以：

1. 打开 AstrBot 的 WeChat Native 测试面板。当 Native 服务正常但微信账号未认证时，面板自动展示登录入口及二维码，扫码后显示确认状态并自动恢复正常视图。
2. 在已经接入 AstrBot 的 **QQ / Telegram / LINE / weixin_oc** 等平台的某个**私聊会话**中输入简单指令，让该会话成为指定 WeChat Native 机器人的告警接收目标。
3. 微信掉线时收到一次消息，恢复时收到一次消息；如果之后再次掉线，收到新一轮消息。
4. 从面板查询连接状态，或通过 `/通知状态` 查询当前绑定与健康状态。

### 1.3 非目标

- 不修改 `agent-wechat` Rust/CLI、AstrBot 内建平台适配器，亦不新建 Telegram/LINE/QQ 专用发送模块。
- 不提供账号登出、删除凭据、创建 Native 账号、服务器重启、Docker 控制、二维码通过聊天发送等功能。
- 不修改现有微信消息收发、历史、群聊、附件、回执或去重流程。
- 不做 LLM Tool Calling、自然语言意图猜测、无斜杠的关键词监控或群聊指令绑定。
- 不实现多接收人广播、邮件/SMS、告警自动重试、定时重复提醒；未来可另开需求。
- 不宣称可监控 AstrBot 自身宕机：内部后台任务随 AstrBot 停止而停止。

---

## 2. 架构

```text
                           AstrBot
    ┌───────────────────────────────────────────────┐
    │  WeChat Native Plugin                         │
    │                                               │
    │  Plugin View (existing connection panel)       │
    │    └─ Plugin Web API                           │
    │         └─ LoginSessionManager                │
    │               └─ Native v2.3.0 login-attempts │
    │                                               │
    │  ConnectionMonitor (async, every 60 seconds)  │
    │    └─ Read-only Native probe                   │
    │         └─ IncidentState (Plugin KV)           │
    │               └─ AlertSender                  │
    │                     └─ Context.send_message()  │
    │                                               │
    │  Cross-platform Command Handlers              │
    │    └─ AlertBindingService                     │
    │         └─ Trusted event.unified_msg_origin   │
    │               └─ Plugin KV                    │
    └───────────────────────────────────────────────┘
                            │
         ┌──────────────────┼──────────────────┐
      weixin_oc           Telegram         QQ / LINE / ...
       私聊会话           私聊会话            私聊会话
```

**原则：** `LoginSessionManager` 与 `ConnectionMonitor` 独立，后者绝不调用前者；通知只使用 AstrBot 公共 `context.send_message()`，不访问第三方适配器的私有 API 或 `context_token` 缓存。

---

## 3. Native v2.3.0 API 契约

来源（固定 tag）：

- [Rust HTTP route table](https://github.com/thisnick/agent-wechat/blob/v2.3.0/packages/native-client-rust/crates/wechat-api/src/http_route_table.rs)
- [Generated contracts](https://github.com/thisnick/agent-wechat/blob/v2.3.0/packages/shared/src/native.generated.ts)
- [中文 API 文档](https://github.com/thisnick/agent-wechat/blob/v2.3.0/docs/src/content/docs/zh-cn/use/api.mdx)

| 用途 | HTTP API（`/v1` 前缀） | 所需 Scope | 说明 |
|---|---|---|---|
| 读取服务状态 | `GET /status` | `read` | `state=ready` 为可用 |
| 读取账号状态 | `GET /accounts/{accountId}` | `read` | 校验 `id`、`authenticated`、`connection` |
| 开始登录 | `POST /accounts/{accountId}/login-attempts` | `login` | 返回 `202 NativeOperation`；发送 `Idempotency-Key` |
| 查询登录状态 | `GET /accounts/{accountId}/login-attempts/{operationId}` | 登录任务需 `login` | 验证任务与账号一致 |
| 获取二维码 | `GET /accounts/{accountId}/login-attempts/{operationId}/qr?generation=N` | `login` | Native PNG 原图，带代次；不得缓存 |
| 取消任务 | `POST /accounts/{accountId}/login-attempts/{operationId}/cancel` | 登录任务需 `login` | 本版无需暴露给 UI，保留可扩展性 |

默认请求体（面板首次进入）：

```json
{
  "strategy": "auto",
  "allowQr": true,
  "forceQr": false,
  "timeoutSeconds": 900,
  "qrRefresh": { "mode": "auto", "maxRefreshes": 3 }
}
```

`strategy=auto`：优先使用已有设备登录能力，仅在需要扫码时显示二维码。二维码来自 Native 返回的 `image/png`，不得自行由文字/ASCII/base64 payload 重新编码成二维码。Native `Operation` 至少涉及 `id`、`accountId`、`kind`、`state`、`result`、时间信息；`result.qrAvailable`、`result.qrGeneration` 需按实际类型严格解析。

**2026-11-12 用户确认的修订：** 用户显式点击“重新获取二维码”且需要创建新任务时，
使用 `strategy=qr`、`forceQr=true`，其余请求字段保持一致，跳过失败的旧会话恢复。
活跃任务或结果不确定的请求继续复用原任务、原幂等 Key 与原策略；不自动切换策略或重复创建。

**兼容限制：** v2.3.0 还提供 `/auth/login`、`/operations/{id}`，但本功能统一走 `/login-attempts` 路由族，不混用两套前端契约；如果未来改版，先变更契约测试。

---

## 4. 连接探针与健康分类

将当前 `ConnectionView.check()` 中的逻辑提取为**可复用、纯只读的异步 Probe**，Web API 与 Monitor 共用同一判定规则。切勿复制两份逻辑。

| Native / Account 状态 | 内部健康分类 | 面板表现 | 是否展示扫码入口 |
|---|---|---|---|
| API 断开、超时、返回 401/403/5xx 或 `state!=ready` | `unavailable` | 显示服务或权限故障 | 否 |
| 账号响应无效 / ID 不匹配 | `unavailable` | 显示配置/响应错误 | 否 |
| `authenticated=false` | `offline` | 微信未登录 | **是**（仅 Dashboard） |
| `authenticated=true`, `connection!=online` | `offline` | 重连中 / 连接异常 | 否，不强制重新扫码 |
| `authenticated=true`, `connection=online` | `online` | 在线 | 否 |

**恢复判定只允许 `authenticated=true AND connection=online`。** HTTP 200、Daemon ready、账号仍已认证，都不能单独视为在线恢复。

接口返回稳定分类与安全原因，不返回 Token、原始服务响应、请求 URL、完整 traceback。现有 `/connection/check` 的对外字段需保持兼容，可额外增设 `health`/`auth_state` 供新版 UI 使用。

---

## 5. Dashboard QR Login

### 5.1 用户交互

1. 打开现有 WeChat Native 连接面板，选中机器人，加载只读状态。
2. `authenticated=false` 且 Native API 可访问：展示扫码登录区域，并**在该面板进入时仅启动/恢复一个**登录任务。若后台尚无 QR，提示“正在准备登录”；若出现 QR，展示 Native PNG。
3. `awaiting_scan`：显示二维码和扫码提示；`awaiting_approval`：提示“请在手机微信中确认”；`succeeded`：重新读取账号连接状态，确认 `online` 后隐藏登录区域并刷新面板。
4. 任务 `expired/failed/rejected/cancelled/needs_login` 等终态：明确提示失败或过期，提供“重新获取二维码”手动操作；不得在失败状态无限重启任务。
5. Native 服务故障、账号已认证但短暂重连或管理员授权失败时，不启动登录。
6. UI 正常刷新保持现有 30 秒节奏；登录任务轮询约 2 秒。页面隐藏暂停轮询，重显时**恢复原任务**。
7. 多机器人切换必须丢弃旧机器人异步回包，防止二维码/状态串线；不可破坏现有统计卡片、最近七日图表与移动端/暗色主题。

### 5.2 新增 Plugin Web API

现有 `connection/bots`、`connection/check`、`connection/panel` 保持不变；新增：

| Endpoint | 方法 | 请求 | 安全响应示例 |
|---|---|---|---|
| `connection/login/start` | POST | `{"bot_id":"native_bot_01"}` | `{"state":"awaiting_scan","qr_available":true,"qr_generation":1}` |
| `connection/login/status` | GET | `bot_id` | `{"state":"awaiting_approval","qr_available":false}` |
| `connection/login/qr` | GET | `bot_id`, `generation` | `{"image":"data:image/png;base64,...","generation":1}` |

- 前端经 `window.AstrBotPluginView.apiGet/apiPost()` 调用；不得浏览器直连 Daemon、直接暴露 Bearer Token 或把含凭据的 URL 放在图片标签上。
- 后端自行解析 Native 二进制响应，校验 Content-Type、PNG signature、字节大小（默认 ≤ 1 MiB）、QR generation 及 Operation 的账号、种类、ID。
- `qr` 响应经现有 Bridge 传输为 Base64 Data URL，仅在内存保留，禁止持久化、打印到日志或上传 CDN。每次 QR 代次变化都替换旧图片。
- `Cache-Control: no-store`；所有 Plugin API 由 Dashboard 身份认证并在服务端做**管理员授权**。`request.username` 非空不代表管理员；必须核对当前 AstrBot 版本的真实角色校验接口。缺少可靠鉴权能力时 **fail closed**，不能仅依赖前端隐藏按钮。
- 仅允许查找服务端已保存、已启用的 `wechat_native` 实例；浏览器不得传 `api_url/account_id/token_file/operationId/strategy` 之类的敏感控制参数。

### 5.3 Login Session 幂等

- 后端每个 `bot_id + canonical api_url + account_id` 仅保留一个活跃登录任务；使用 per-bot `asyncio.Lock` 防止面板重复打开或双击发起多次 POST。
- 插件 KV 持久化 `idempotency_key`、`operation_id`、创建时间、任务标识、`force_qr` 策略和终态；**先提交幂等 Key 与策略再调用 Native POST**。请求超时或响应丢失时复用原 Key 及原策略，不得产生新的登录任务。旧记录缺少 `force_qr` 时按 `false` 恢复。
- 若 KV 已有有效 `operation_id`，先查询任务状态；刷新、浏览器重开、AstrBot 重启均恢复已有任务。
- 任务为终态后仅在用户显式要求“重新获取二维码”时创建新的幂等 Key；Native 报 `409 operation_in_progress` 时不要盲目重建或取消未知任务。
- 原有 `admin_token_file`（默认 `/run/secrets/wechat-admin-token`）拥有目标账号 `admin` 权限，可调用 Native 的 `login` scope 路由；普通 `token_file` 继续用于读状态与消息链路。**不得添加第三个 Token/Secret 或用普通 Token 越权兜底**。
- URL/account/机器人配置更改后，不得沿用旧账号登录 Operation；持久化 key 必须包含配置标识（敏感字段做哈希，不记录 Token 内容）。

---

## 6. 跨平台通知指令

### 6.1 交互（中文优先）

| 中文指令 | 英文主命令 / 别名 | 含义 |
|---|---|---|
| `/绑定通知 [native_bot_id]` | `/bind_alert [native_bot_id]` | 将**当前私聊会话**设置为该 Native 机器人的通知接收目标 |
| `/解绑通知 [native_bot_id]` | `/unbind_alert [native_bot_id]` | 移除此 Native 机器人的通知目标 |
| `/通知状态 [native_bot_id]` | `/alert_status [native_bot_id]` | 查看健康状态、绑定平台与最近一次通知尝试结果 |
| `/测试通知 [native_bot_id]` | `/test_alert [native_bot_id]` | 显式发送一次测试消息，不改变事故状态 |

**命令注册要求：** 使用 `@filter.command("bind_alert", alias={"绑定通知"})` 等等；英文为注册主命令、中文为 `alias`，同一 Handler 实现。Telegram Bot 命令菜单只接受 ASCII 小写、数字、下划线，中文 Alias 不必出现在菜单中，但支持文本消息直接输入。禁止写两套中英业务函数。

**省略 ID 规则：** 当前只有一个已启用 Native 机器人时，`/绑定通知` 即绑定它；如果多个，必须明确写 ID，例如 `/绑定通知 native_bot_01`，否则返回候选 ID 列表。无匹配机器人返回清晰错误，不猜测。

**指令场景示例：**

```text
在 Telegram 私聊机器人：
/绑定通知 native_bot_01
→ ✅ 已绑定，通知将发送到当前 Telegram 私聊。

在微信 ClawBot 私聊：
/绑定通知 native_bot_02
→ ✅ 已绑定，通知将发送到当前 ClawBot 私聊。

/通知状态 native_bot_02
→ Native: Offline；通知渠道: weixin_oc；已绑定；上次告警已尝试。
```

### 6.2 权限及平台识别

- 绑定、解绑、查询、测试均要求已启用平台的**私聊**且 `event.is_admin()` 为真；推荐叠加 `@filter.permission_type(filter.PermissionType.ADMIN)`。群聊、非管理员、无可靠权限信息，必须拒绝。
- 平台类型由 `event.get_platform_name()` 获取，例如 `weixin_oc`、`telegram`、`line`、`aiocqhttp`；**机器人实例 ID** 由 `event.get_platform_id()` 获取；目标会话由 `event.unified_msg_origin` 获取。不能从昵称、微信号、Telegram handle 或用户传入的自定义 UMO 猜测。
- 禁止绑定群会话作为接收人。本功能不支持通过一个平台自动推断另一平台的用户身份；**在哪个平台的私聊执行绑定，就通知到那个会话**。
- 命令层仅处理来自真实 AstrBot 入站事件的可信值，不信任用户直接输入平台 ID、Session ID 或机器人实例 ID（唯一允许的参数是要监控的 Native bot ID）。
- 发送前按持久化实例 ID 使用 `context.get_platform_inst(platform_id)` 验证适配器仍存在、启用并且平台类型一致；不做跨平台 fallback。

### 6.3 绑定持久化

使用 AstrBot Plugin KV；一台 Native 机器人最多一条绑定，重复绑定覆盖旧目标，互不影响：

```json
{
  "key": "connection_alert_binding:native_bot_01",
  "value": {
    "schema_version": 1,
    "platform_type": "telegram",
    "platform_id": "telegram_main",
    "umo": "<event.unified_msg_origin 原样存储>",
    "bound_at": "2026-11-12T10:00:00Z"
  }
}
```

`umo` 保留 `event.unified_msg_origin` **原始值**，绝不手动构造或猜测格式。绑定必须先验证并**成功写 KV**才回复“已绑定”；重复执行同一绑定应幂等。解绑只删除目标 KV，不删除事故状态；再次绑定不追溯补发当前事故已经发生的告警。

状态指令不能把完整 UMO、用户 ID、群号、Secret 或其他敏感定位值打印出来。管理命令的目标 Native bot ID 是指令参数，不是要发送通知的平台 ID。

### 6.4 统一发送接口

```python
from astrbot.api.event import MessageChain

# saved_umo 是绑定指令时的 event.unified_msg_origin 原值。
# 发送前需要校验平台实例仍然存在且类型匹配。
sent = await self.context.send_message(
    saved_umo,
    MessageChain().message(
        "⚠️ WeChat Native 连接异常\n"
        "机器人：native_bot_01\n"
        "请打开 AstrBot 测试面板检查连接或扫码登录。"
    ),
)
if not sent:
    raise RuntimeError("未找到通知目标平台实例")
```

`context.send_message()` 返回 `False` 或抛异常属于失败，返回 `True` 仅代表找到平台并完成适配器发送调用，**不保证最终送达、已读或长期有效**。对 `weixin_oc`，有效的 `context_token` 仍是前提；LINE、QQ 官方等可能有主动消息配额与政策限制。

---

## 7. 后台监控与告警状态机

### 7.1 检测节奏

- `initialize()` 启动后台 asyncio 任务；`terminate()` 可靠取消并 await。不能在构造函数阻塞或依赖面板打开。
- 每 **60 秒**检查所有 `enable=true` 的 `wechat_native` 实例；单次 HTTP 探针超时 **5 秒**。单实例不允许重叠探针；异步实现不能阻塞消息处理主循环。
- 依据第 4 节的唯一健康判断逻辑，`online` 为成功，`offline/unavailable` 均记一次失败，保留分类原因供文案与诊断。
- Native 机器人被禁用或移除时停止该机器人的探测和发送；机器人身份（URL/account）变更时应重建基线，而不是从旧账号事故产生“恢复通知”。
- **没有通知绑定目标也执行监控状态机，但不发送任何消息。**

### 7.2 阈值与状态迁移

| 当前状态 | 观测 | 结果 |
|---|---|---|
| `UNKNOWN` | 首次在线 | 建立 `ONLINE` 基线，不发送恢复通知 |
| `UNKNOWN/ONLINE` | 异常不足 3 次 | 累计失败计数，不发送 |
| `UNKNOWN/ONLINE` | **连续第 3 次异常** | 创建 `incident_id`，进入 `INCIDENT_OPEN`，尝试 **1 条断线通知** |
| `INCIDENT_OPEN` | 持续异常 | 不重复发送；恢复计数归零 |
| `INCIDENT_OPEN` | 首次在线 | 恢复计数 +1，不发送 |
| `INCIDENT_OPEN` | **连续第 2 次在线** | 标记事故已恢复，尝试 **1 条恢复通知**，回到 `ONLINE` |
| `ONLINE`（恢复后） | 下一次连续 3 次异常 | 新的 `incident_id`，新一轮断线及恢复通知 |

正常/异常的连续计数必须在相反结果出现时清零；不能使用时间累计等价替代“连续次数”。重启后恢复持久化事故和计数状态，防止每次重启重新通知。

### 7.3 严格的通知去重

- 每个 Native 机器人持久化以下业务状态：

```json
{
  "schema_version": 1,
  "bot_id": "native_bot_01",
  "identity_hash": "<stable hash of server URL + account ID>",
  "phase": "INCIDENT_OPEN",
  "incident_id": "<uuid>",
  "failures": 3,
  "recoveries": 0,
  "failure_kind": "unauthenticated",
  "outage_attempted": true,
  "recovery_attempted": false,
  "last_outage_attempt": "2026-11-12T10:00:00Z",
  "last_recovery_attempt": null
}
```

- 以 `(native_bot_id, incident_id, notification_kind)` 作为逻辑去重键；**在调用发送前先持久化 attempted 标记**，KV 写入失败则跳过发送并记录安全日志。
- 同一事故断线、恢复**各最多尝试一次**。调用失败也算已尝试，**不自动重试、不补发**。这是一种 **at-most-once attempt** 语义，优先避免重复通知，接受崩溃窗口可能漏发。
- 同一 bot 运行期间序列化状态转换与发送判断，避免“两个监控任务同时发现断线”导致双发；本版仅支持单个 AstrBot 活跃插件实例，多实例部署需要分布式锁/CAS，属于非目标。
- 没绑定时记录 `skipped_no_binding`，不将旧事故延后发送给后来绑定的接收人；恢复事件发生时有新绑定则按当时目标尝试一次恢复通知。
- 绑定切换、目标平台下线、通知 API 出错、AstrBot 重启均不得反复补发当前事件；恢复并建立下一轮事故后才允许下一轮发送。

### 7.4 通知文本

断线（一次事故最多一条）：

```text
⚠️ WeChat Native 已断线
机器人：native_bot_01
原因：微信账号未登录 / 微信连接异常 / Native 服务不可用
检测：连续 3 次异常
请打开 AstrBot 的 WeChat Native 测试面板检查。
```

恢复（一次事故最多一条）：

```text
✅ WeChat Native 已恢复
机器人：native_bot_01
状态：微信已重新上线
```

测试（用户显式触发）：

```text
🔔 WeChat Native 通知测试
机器人：native_bot_01
这是一条测试消息，不代表连接出现故障。
```

消息由确定性模板构造，不经 LLM，不触发机器人之间的自动聊天/工具调用回路。`/测试通知` 不受事故去重限制，也**不改变监控状态**。

---

## 8. 配置与安全

### 8.1 保留现有配置

```yaml
# 每个 Native 机器人沿用原配置；以下仅为示意
server_url: http://native-service:6174
account_id: "<Native account ID>"
token_file: /run/secrets/wechat-api-token
admin_token_file: /run/secrets/wechat-admin-token
```

**明确不得新增：** `login_token_file`、`enable_connection_monitor`、`alert_platform_bot_id`、`alert_weixin_oc_bot_id`、`alert_weixin_oc_user_id`、`notification_umo`、`notification_session_id` 等用户配置项。所有通知目标只在管理员执行指令后写入 Plugin KV；没有绑定时保持无通知状态。

`config.py` 仅可按需要更新既有 `admin_token_file` 的说明文字，标注其用于扫码登录和已有管理员操作；不增加同义配置项。

### 8.2 必须满足的安全措施

1. Dashboard 登录/QR Web API 为特权接口，必须有**服务端真实管理员授权**，不能只判断 `request.username != None` 或页面已登录。
2. 聊天指令只信任来自已鉴权 AstrBot 平台事件的管理员身份，群聊、未识别/无管理员权限的私聊直接拒绝，不提供可绕过的通用 webhook。
3. `admin_token_file` 仅后端读取；禁在浏览器、Plugin KV、前端持久化存储、测试截图、日志、报错正文中泄漏 Token。
4. Native API 只走 `NativeConfig` 校验过的服务地址及账号，不允许用户通过 HTTP 请求直接传 arbitrary host/path，避免 SSRF。
5. 禁止 QR 明文写磁盘/日志，使用 `Cache-Control: no-store`、限制图片大小、超时、响应类型，不跟随重定向。
6. `context.send_message()` 发送失败仅在日志记录**脱敏**错误，不记录完整 UMO、上下文令牌、平台用户 ID；禁止重试风暴。
7. 现有微信消息处理逻辑保持原样；监控或命令失败不能让正常 Adapter 崩溃。
8. 启动时恢复绑定与事故记录时校验 schema/version/字段类型；坏记录安全跳过并记录脱敏诊断。

---

## 9. 代码职责与建议文件范围

| 文件 | 修改内容 |
|---|---|
| `main.py` | 注册登录专用 Web API；注册四个英文主命令及中文别名；在 `initialize()/terminate()` 管理监控任务 |
| `connection_view.py` | 复用 Probe；加入管理员授权的 `login/start`、`login/status`、`login/qr` 接口；面板可展示是否绑定 |
| `native_client.py` | v2.3.0 Login Attempts 的 POST/GET/PNG 方法，严格错误处理和 MIME/字节数校验 |
| `config.py` | 保持配置字段不变；必要时修改既有 Admin Token 描述 |
| `views/connection/index.html` | 增加二维码登录 UI，不破坏现有统计信息 |
| `views/connection/app.js` | 登录状态轮询、QR generation 更新、页面可见性/切换机器人处理、Bridge API |
| `views/connection/style.css` | QR 卡片、响应式、暗色主题 |
| **新增** `connection_probe.py` | 单一只读健康检测及 `online/offline/unavailable` 分类 |
| **新增** `login_session.py` | 登录 Operation 幂等协调、持久化与并发锁 |
| **新增** `connection_monitor.py` | 后台监控循环、阈值/事故状态机、持久化去重 |
| **新增** `alert_binding.py` | 命令的权限校验、默认 Native 选择、跨平台绑定与 Plugin KV |
| **新增** `alert_sender.py` | `context.send_message()` 的统一路由、格式化通知、发送结果记录 |
| `tests/*` | Native API 契约、后端监控/命令权限、UI Playwright、回归测试 |
| `README.md` / 操作说明 | 中英命令、扫码入口、平台限制、部署与排错 |

文件划分仅建议；如仓库现有结构已有等效服务，可直接复用，避免为简单职责引入冗余抽象。

---

## 10. 测试计划（全部自动化场景使用 mock，真实验收单独列出）

### 10.1 API & QR

- **QR-01** Native v2.3.0 路由、HTTP 202、Bearer 权限、`Idempotency-Key` 与请求体正确。
- **QR-02** `authenticated=false → awaiting_scan → awaiting_approval → succeeded → account online`，二维码只在有效期显示。
- **QR-03** `authenticated=true + connection=reconnecting`、Daemon 不可达、401/403 时**不创建登录任务**。
- **QR-04** `qrGeneration` 变化正确替换 PNG；遇到 `409 superseded`、图片损坏、超大响应、非 PNG 响应，安全提示而非显示错误内容。
- **QR-05** 同一账号多标签页、双击、请求丢失、页面刷新、AstrBot 重启只保留一个未完成登录任务；终态新开由用户显式触发。
- **QR-06** 多 Native 机器人切换后不会混用旧二维码、Operation 或 Secret；面板原有检查与统计功能不回归。
- **QR-07** 未登录 Dashboard、非管理员、伪造 bot ID、非法参数、缺失 Admin Token 的访问被拒绝；旧只读检测仍可用。

### 10.2 跨平台指令与绑定

- **CMD-01** 四个中文命令与四个英文主命令命中**同一业务逻辑**；Telegram 菜单英文注册可用。
- **CMD-02** 单个已启用 Native 机器人可省略 ID；多个时必须传 ID，未传时展示可选 ID；不存在/禁用 ID 拒绝。
- **CMD-03** Telegram、`weixin_oc`、LINE、QQ OneBot 私聊中的可信事件能正确捕获 `platform_type/platform_id/umo`，绑定值可重启恢复。
- **CMD-04** 非管理员、群聊、伪造平台实例/会话参数、命令名碰撞等场景拒绝或安全处理；不泄露完整 UMO。
- **CMD-05** 再次绑定覆盖、重复绑定幂等、解绑不影响其他机器人；两个 Native 实例可绑定不同平台，或共享同一通知私聊。
- **CMD-06** `/测试通知` 只发送一次测试消息，不更改事故状态；平台不存在、上下文令牌过期、主动消息受限、返回 `False` / 抛异常时准确报告尝试失败。

### 10.3 连接监控与事故去重

- **MON-01** 连续两次故障不告警；第三次故障最多一次；后续故障任意多次不重复。
- **MON-02** 进入事故后一次健康不恢复；第二次连续健康最多一次恢复消息；下个故障周期重新告警。
- **MON-03** 短暂恢复后又故障，恢复计数清零；短暂故障后在线，失败计数清零。
- **MON-04** 首次启动健康不发“恢复”；首次启动异常累计 3 次才发“断线”；插件重载后保持状态不重复告警。
- **MON-05** 通知目标缺失、发送失败、KV 持久化错误、进程重启、临时网络异常均不引发自动重复通知；失败可诊断。
- **MON-06** 发生事故后重新绑定，不回补当前事件；机器人禁用或账号身份改变不产生伪恢复消息。
- **MON-07** `terminate()` 后不存在残留监控 Task；多个机器人检查互不阻塞；监控只读且不会自动打开登录任务。

### 10.4 实际集成验收（人工）

1. Native v2.3.0 + 当前 AstrBot：真实二维码扫码、手机确认、账号恢复在线，面板状态及统计仍正常。
2. 真实 `weixin_oc`、Telegram、QQ OneBot、LINE：管理员私聊中执行中文命令（无法支持中文 Slash 的平台须验证英文等价命令）；执行 `/测试通知` 验证主动消息实际收到。
3. 模拟：断线 → 连续三次检查 → 一条断线告警 → 恢复两次检查 → 一条恢复通知 → 再次断线 → 新告警。
4. 重启 AstrBot、重新打开面板、多标签页场景验证：不重复登录、不重复告警。
5. 若某平台主动发送受服务端限制，**明确记录为环境限制**，不能把 API 调用成功等同消息到达。

---

## 11. 实施阶段与交付标准

| Phase | 目标 | 验收 Gate |
|---|---|---|
| **1. Contract / RED** | 仔细检查当前仓库与 v2.3.0 tag；补失败的 Native API、监控状态机、命令权限测试 | 测试与需求一一对应 |
| **2. Login Backend / GREEN** | Probe、NativeClient Login Attempts、Login Session KV、Web API 授权 | API/并发/安全测试通过 |
| **3. Dashboard / GREEN** | 扫码 UI、登录轮询、扫码过期和确认、多机器人切换 | Playwright 通过，现有 UI 不回归 |
| **4. Cross-platform Alerts / GREEN** | 命令中文 Alias、绑定 KV、Monitor、AlertSender、去重 | 单元与集成矩阵通过 |
| **5. Hardening & Release** | pytest、Ruff、Playwright、README、人工 v2.3.0 集成验收说明 | CI 绿；未完成环境验收明确列出 |

### Definition of Done

- [ ] 不新增用户通知配置字段、不新增 Docker Secret、没有 `login_token_file`。
- [ ] 现有 Admin Token 可完成 v2.3.0 登录流程；普通消息依旧只用普通 API Token。
- [ ] 未认证时面板显示扫码登录；已认证但断线时只等待 Native 自恢复而非盲目扫码。
- [ ] 中文与英文指令在支持的平台均可绑定同一个实际私聊，UMO 完整持久化。
- [ ] 每个 Native bot 可绑定一个通知目标，跨平台实例路由正确。
- [ ] 每个 incident 最多一次断线尝试、一次恢复尝试；重启不重复；下一次 incident 可再次告警。
- [ ] 后台监控与面板无依赖；关闭页面仍有状态检查；通知失败不影响微信主消息链路。
- [ ] 服务端管理员权限得到验证；无 Secret、二维码、完整用户标识泄漏。
- [ ] 既有 pytest、Ruff、Playwright 与新增测试全部通过；真实环境尚未运行的项目明确标为待验证。
- [ ] README、变更日志、使用教程和测试报告更新；代码提交到功能分支并说明 commit/PR。

---

## 12. 可直接交给开发 Agent 的执行提示

```text
Implement the complete feature in neozhu/astrbot-plugin-wechat-native according to
SPEC-wechat-native-connection-recovery-v2.3.0.md (Revision 2.0).

Compatibility baseline: thisnick/agent-wechat tag v2.3.0.
Start by inspecting existing code/tests and verifying tag-specific HTTP contracts.
Use TDD and a small number of maintainable, independently testable services.

Requirements:
1. Add QR login to the existing AstrBot connection panel using v2.3.0
   login-attempts APIs, with backend-only admin-token access, persistent
   idempotency, one active login task per bot/account and safe PNG handling.
2. Run a read-only background health monitor every 60s with thresholds
   3 consecutive failures / 2 consecutive successes.
3. Implement four platform-agnostic admin-only private-chat commands:
   /bind_alert (alias /绑定通知), /unbind_alert (alias /解绑通知),
   /alert_status (alias /通知状态), /test_alert (alias /测试通知).
   Omit the native bot ID only when exactly one enabled bot exists.
4. Capture the trusted incoming platform instance ID and unified_msg_origin;
   persist exactly one alert binding per native bot in AstrBot Plugin KV.
5. Route all notifications through context.send_message(saved_umo), not through
   platform-specific internal clients. Treat external delivery as best effort.
6. Persist incident state, mark send-attempted BEFORE sending, never retry the
   same outage/recovery automatically, and re-arm after a new incident.
7. Never add alert target, platform ID, UMO, monitor enable, or login token
   configuration fields. Reuse existing admin_token_file and token_file.
8. Keep all existing message reception/sending, group tools, statistics,
   dashboard visuals, and session journal behavior unchanged.
9. Enforce real Dashboard admin authorization on login/QR APIs; enforce
   AstrBot administrator + private-chat checks for all binding commands.
10. Add full pytest, Ruff and Playwright coverage, update README and tests.

Do not modify agent-wechat or AstrBot core. Do not invoke the wx CLI.
Report exact test commands/results and any real-account acceptance steps
that cannot be tested in this environment. Never claim untested delivery works.
Create a feature branch, commit completed changes, and give a final summary.
```

---

**版本参考：** [agent-wechat v2.3.0](https://github.com/thisnick/agent-wechat/releases/tag/v2.3.0) · [AstrBot 主动消息文档](https://github.com/AstrBotDevs/AstrBot/blob/master/docs/zh/dev/star/guides/send-message.md) · [AstrBot 插件 Views 文档](https://github.com/AstrBotDevs/AstrBot/blob/master/docs/zh/dev/star/guides/plugin-pages.md)
