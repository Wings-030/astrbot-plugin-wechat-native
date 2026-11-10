# Group Pat Reply Implementation Plan

**Goal:** Native v2.2.1 补充拍一拍身份，插件读取前一条非机器人图文/语音并按人格回应。
**Spec:** ../specs/2026-11-11-group-pat-reply-design.md
**Execution:** 当前会话直接实施；用户负责部署和日志调试，不再分派任务或扩大调查。

- [x] 插件：先增加准入、历史选择、媒体、语音转写和冷却行为测试，再实现 config/pat/adapter/main 接入。按用户要求仅保留 5 个测试。
- [x] Native：基于精确 v2.2.1 归档生成可应用补丁，包含受限 XML 解析、消息投影、可选 pat 契约和诊断日志；本机无 Rust，不声明编译通过。
- [x] 交付：5 个 pytest 通过，ruff 检查通过；补丁应用检查通过。Native 编译未验证，部署命令见 native-patches/README.md。

不安装 Rust/Docker、不推送远端、不代替用户部署。原地修改当前插件；Native 修改仅在临时隔离副本中完成，以补丁文件交付。
