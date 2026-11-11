# 谁最能聊：结果展示与重复查询验证

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 让当日榜单的统计时段、总量、排名、话题和点评更清楚，并核实同日新指令会重新查询。

**Architecture:** 沿用 `collect_ranking` 的请求内统计和 Native HTTP 查询；在 `DailyRanking` 中保留本次统计截止时间及全部合格记录的总量，由代码生成概览。模型仍只生成话题和点评。

**Tech Stack:** Python 3.12、AstrBot SDK、aiohttp、pytest。

**Spec:** [谁最能聊](../../SPEC-daily-group-ranking.md)。本次调整见该规范的“结果展示与查询诊断”部分。

## Constraints

- Top 5、连续 30 秒合并发言、真正 @、当前账号/群范围及单条纯文本回复保持现有规则。
- 不增加配置、数据库、定时任务、榜单缓存或跨日指令。
- 历史最多 20 页，每页 50 条，总时限 15 秒；模型一次调用，时限 8 秒。
- Native v2.3.0 的首个历史请求不携带游标；后续页只使用本次查询返回的游标。
- 不改动此前 README 图示及部署步骤；不宣称完成微信实机验证。

## Tasks

- [x] 增加同群早晚两次触发的真实 HTTP 测试，核实第二次重新从第一页开始并读取新增消息。
- [x] 在测试中约定统计时段、全部参与成员与发言/消息总量；覆盖超过 Top 5 的成员与部分统计。
- [x] 为模型代码围栏 JSON 和 Native `coverage.partial` 增加行为测试，先确认失败。
- [x] 在 `group_ranking.py` 中添加统计概览、查询结果日志、部分覆盖标记和有限的 JSON 围栏兼容；调整话题点评提示。
- [x] 更新既有格式断言与功能指南示例，说明每条新指令独立查询及真实环境排查边界。
- [x] 运行排行测试及受影响客户端/摘要测试，运行修改文件的 Ruff 检查。

## Verification

- `pytest -q tests/test_group_ranking.py tests/test_summary.py tests/test_native_client.py`：109 passed。
- `ruff check group_ranking.py tests/test_group_ranking.py`：通过。
- `ruff format --check group_ranking.py`：通过；测试文件保留既有列表排版。
- 早晚两次查询的测试在改动前已通过，未复现插件复用早上榜单的问题。
- 新概览、部分覆盖标记与 JSON 围栏的 3 项测试先失败、实现后通过。
- 未连接用户的 Native v2.3.0 服务或真实微信，实机结果不变的原因仍需结合新增日志核实。
