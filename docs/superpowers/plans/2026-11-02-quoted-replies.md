# Quoted Replies Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans; implement and verify in the current workspace.

**Goal:** 支持 Native 原生文本引用回复，并把收到的引用文本交给 AstrBot。

**Architecture:** 将 AstrBot Reply 组件转换为 Native quote（从同账号同聊天读取 messageId/serverId）。收到 type=49/appMessageType=57 时转成 Plain + Reply 组件，不读取或递归展开引用来源。

**Tech Stack:** Python 3.12, AstrBot >=4.29.3,<5, Native v1.3.3, pytest/aiohttp.

**Spec:** 本文件包含本次小功能设计；需求为用户追加的“通过后添加消息引用回复”。

## Approaches

- Native quote（采用）：保留微信原生引用，沿用 AstrBot 的 reply_with_quote 开关及 Native 去重发送。
- 拼接引用文本：实现容易但没有原生引用关系，不采用。

## Global Constraints

- 只发送非空文本引用，最大 16 KiB UTF-8；Native 自行验证支持的原消息类型。
- quote 只传 messageId/serverId，不信任 Reply 组件携带的正文、昵称等内容。
- 禁止跨账号、跨聊天、撤回和缺失 serverId 的引用；发送前重新核对消息和状态。
- Native v1.3.3 支持 quote + mentions；保留显式 @及原有群内首段自动 @。多引用目标报错。
- 原生引用仍属 Native 实验能力，真实手机渲染须部署后验证。
- 接收引用文本只读取 Native quote 的 embeddedText；recalled/missing/unsupported 不暴露所携带的旧正文，显示相应提示。

## Review Focus

- Reply 不得继续被静默丢弃；私聊和群聊均发送原生 quote。
- 接收引用消息遵守现有 live、白名单和 @触发规则。
- 引用发生额外读取时不能绕过源消息撤回及发送失败门控。
- 16 KiB 按 UTF-8 字节计算，serverId 保持字符串。
- 失效引用不能留下已提交的发送回执；引用与 @应同时传给 Native。

### Task 1: 引用发送

**Files:** `event.py`, `adapter.py`, `native_client.py`, `tests/test_quotes.py`.

**Interfaces:** `serialize_text` 返回 `(text, mentions, reply_id)`；`NativeClient.send_text(..., *, quote=None)` 使用相同 POST 和回执恢复机制。

- [x] 写私聊/群聊引用、不同目标、非法引用、@组合、大小和撤回测试，确认失败。
- [x] 接入原生 quote、消息身份校验及最终发送门控。
- [x] 运行引用及现有 adapter/client 测试。

### Task 2: 引用接收与说明

**Files:** `event.py`, `adapter.py`, `tests/test_quotes.py`, `README.md`.

- [x] 写接收引用上下文、未知/撤回引用、不支持类型及群触发测试，确认失败。
- [x] 转换 Native 引用组件，明确不支持的引用内容。
- [x] 更新启用方法与 Native 原生能力限制；运行完整测试及 ruff。

验证：完整项目 169 项通过，ruff check/format 通过；引用测试 29 项。
独立只读审查确认 Native 协议和 AstrBot ReplyChainParser 兼容，未发现具体缺陷。
未连接真实微信或收费模型服务，手机显示及真实摘要质量需部署后验证。

按用户后续修正，插件版本升为 v0.2.0，AstrBot 最低版本保持 4.29.3。
Native 目标版本更新为 1.3.3；依据该版本官方协议移除引用与 @组合的旧限制，
覆盖显式 @、自动 @及带 @引用发送的原幂等键恢复。

协议依据：
- https://github.com/thisnick/agent-wechat/blob/v1.3.3/docs/src/content/docs/use/api.mdx#send
- https://github.com/thisnick/agent-wechat/blob/v1.3.3/docs/src/content/docs/use/cli.mdx#quoted-replies
- https://github.com/thisnick/agent-wechat/blob/v1.3.3/docs/src/content/docs/use/groups.mdx#mention-group-members
