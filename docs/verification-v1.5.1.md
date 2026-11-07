# Native v1.5.1 开发验证记录

日期：2026-11-07。开发基线：`57e7cecd6bf55d9c9d2e9c0b12416c35d40ed2d3`。
本次在现有插件内实现 7 个工具与 Adapter 内置 Loop Breaker，插件版本为 0.3.0。

## 任务与提交

| Issue | 交付 | 开发提交 |
|---|---|---|
| [#2](https://github.com/neozhu/astrbot-plugin-wechat-native/issues/2) | 硬限额、统一门控与持久化静默冷却 | `3979e06` |
| [#3](https://github.com/neozhu/astrbot-plugin-wechat-native/issues/3) | 文本指纹、可信触发关联和重复往返检测 | `7d3c5c0` |
| [#4](https://github.com/neozhu/astrbot-plugin-wechat-native/issues/4) | 可信上下文、最近消息和安全 JSON 边界 | `d5d43e2` |
| [#5](https://github.com/neozhu/astrbot-plugin-wechat-native/issues/5) | 当前聊天子串搜索 | `0e1365e` |
| [#6](https://github.com/neozhu/astrbot-plugin-wechat-native/issues/6) | 有界群资料、未知值保留 | `6f7094c` |
| [#7](https://github.com/neozhu/astrbot-plugin-wechat-native/issues/7) | 群成员分页及当前页名称筛选 | `09887c4` |
| [#8](https://github.com/neozhu/astrbot-plugin-wechat-native/issues/8) | 自身群昵称、独立管理凭据和持久化写意图 | `49dcf49` |
| [#9](https://github.com/neozhu/astrbot-plugin-wechat-native/issues/9) | 管理员群改名和分层权限错误 | `39974f4` |
| [#10](https://github.com/neozhu/astrbot-plugin-wechat-native/issues/10) | 精确撤回本账号当前聊天的 outgoing 消息 | `a67bcb1` |
| [#11](https://github.com/neozhu/astrbot-plugin-wechat-native/issues/11) | SDK 注册契约、跨功能回归、配置/运维/兼容说明 | 本报告所在集成提交 |

## 已执行验证

环境：Windows，Python 3.12.16，AstrBot 4.29.3（开发依赖锁定官方源码提交），
aiohttp 3.14.5，Ruff 0.15.7。新建隔离开发环境，未使用真实微信凭据。

- `python -m pytest -q`：581 passed，1 条上游 audioop 弃用警告。
- `python -m ruff check .`：通过。
- `python -m ruff format --check .`：通过，28 个 Python 文件格式一致。
- 对全部插件 Python 模块执行 `python -m compileall -q`：通过。
- `git diff --check`：通过。

局部验证随各任务运行，再完成全量回归。测试包含现有 Chromium 连接页面、SSE/游标/去重、
引用/撤回、媒体/语音/PDF、摘要测试；新增真实 SDK 工具注册契约和模拟 Native HTTP 场景。
工具开关关闭与非 Native 事件的零请求、跨聊天响应/伪造上下文拒绝、分页绑定、中文搜索、
历史截断/撤回占位、群主未知和成员当前页筛选均有测试。

写操作验证了管理员白名单、scope 与权限拒绝、提交前持久化、并发/重复调用单次 mutation、
昵称限流、202 pending、丢失响应按原 Key 只读恢复、重启恢复、精确字符串 serverId、
非本人/跨账号/跨群/无 serverId 拒绝、已撤回、含糊目标与私聊撤回的未知回执。
循环保护验证了重复往返、不同内容达到硬限额、无出站群刷屏、冷却持久化/过期、
队列中迟到回复和文本/图片/语音统一阻断，以及不覆盖已存在的 needs_review 状态。

Ruff 固定在已验证的 0.15.7，避免后续格式器版本自动引入 Markdown 示例格式变更。

## 实机与恢复限制

这些结果证明模拟 HTTP 与指定 SDK/本地环境下的行为，不证明微信实机效果。
尚未部署生产账号、执行真实群改名/昵称修改或消息撤回。

群管理使用独立 admin_token_file，缺少管理凭据时返回 configuration_error；撤回沿用原有
read/send 凭据。权限仍由 Native 与微信最终判断，LLM 无法选择别的账号或聊天。

Native v1.5.1 没有撤回操作的按幂等 Key 读取接口。若首次撤回响应丢失且没有 Operation ID，
状态保持 needs_review，等待人工核查，绝不重复 POST。已有 Operation ID 可只读恢复。
HTTP 202、pending、未知回执和本地投影状态不冒充已经成功。

每条触发消息最多提交一次写操作；重复调用会标明 replayed，返回原操作状态。
同群有未决群管理操作时，先核查旧操作，不把旧操作的成功误报为新参数已执行。
硬限额可能误拦正常高频聊天；冷却与指纹始终按账号/聊天隔离。
