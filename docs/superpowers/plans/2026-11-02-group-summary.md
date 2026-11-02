# Group Summary Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task with test-first verification.

**Goal:** 群内 @机器人发送“总结群聊”后，按当前群的有限实时记录生成摘要。

**Architecture:** Adapter 缓存通过实时及群策略校验的文本；独立 summary 模块管理内存范围和结构化提示词。插件命令使用 AstrBot 当前会话模型并沿用原有发送链路。

**Tech Stack:** Python 3.12, AstrBot 4.29.3, pytest, aiohttp.

**Spec:** `docs/superpowers/specs/2026-11-02-group-summary-design.md`

## Global Constraints

- 只改摘要所需文件，无新增运行依赖。
- 摘要默认关闭；缓存不落盘，群、账号和机器人实例隔离。
- 保留现有回复、去重、撤回和权限链路。

## Review Focus

- 普通群消息不能因为开启摘要而触发 AI 回复。
- 白名单、历史、重复和撤回消息不能混入摘要。
- 大消息、多群和长时间运行必须有缓存及输入上限。
- 命令只处理 Native 群内真正 @机器人，生成中撤回不能发出旧摘要。
- 模型错误、空回复及并发请求不能泄漏错误详情或重复回复。

### Task 1: 有限缓存与接收集成

**Files:** Create `summary.py`, `tests/test_summary.py`; modify `config.py`, `adapter.py`, `tests/test_config_state.py`.

**Interfaces:** `GroupSummaryCache.add(raw)`, `snapshot(thread)`, `recall(thread, server_id)`, `contains(records)`, `clear()`；`summary_prompt(records)` 返回有限的 JSON 记录与范围说明。

- [x] 写配置校验、缓存范围和真实 Adapter 接收的失败测试，运行确认缺少功能。
- [x] 增加开关、条数及时间参数；实现有限缓存并接入 fresh 消息、撤回和停止。
- [x] 运行 `python -m pytest -q tests/test_summary.py tests/test_config_state.py tests/test_adapter.py`。

### Task 2: 摘要命令与使用说明

**Files:** Modify `main.py`, `README.md`, `docs/operations.md`, `tests/test_summary.py`.

**Interfaces:** 插件 `summarize_group(event)` 命令；使用 Task 1 的缓存快照及提示词。

- [x] 写从 Native 接收、命令过滤、模型请求到发送的测试，以及空缓存、关闭、错误、超时、并发、撤回测试，先运行确认失败。
- [x] 注册“总结群聊/群聊摘要”命令，调用当前会话模型，限制请求时间和同群并发，返回文本并阻止二次普通回复。
- [x] 更新机器人参数、使用例子、缓存范围和运维说明。
- [x] 运行摘要及现有 Adapter/config 测试、ruff 和完整项目测试，检查最终差异。

验证：完整项目 140 项通过；ruff check/format、git diff --check 通过。独立只读审查未发现具体缺陷。
后续按用户追加请求实现 Native 原生引用回复。
