# Native v2.2.1 拍一拍补丁

此补丁给已识别的群拍一拍消息增加 `pat: {actorId, targetId}`，继续通过 `message.committed` 交付。
支持候选格式 `sysmsg/pat` 和 `49/62 appmsg/patMsg`；未识别的通知不猜测身份。
原版 v2.2.1 不提供此字段，只升级插件不足以启用本功能。

## 在构建机器上操作

```bash
git clone --branch v2.2.1 --depth 1 https://github.com/thisnick/agent-wechat.git agent-wechat-pat
cd agent-wechat-pat
git apply --check /path/to/astrbot-plugin-wechat-native/native-patches/v2.2.1-pat.patch
git apply /path/to/astrbot-plugin-wechat-native/native-patches/v2.2.1-pat.patch
docker build -f deploy/native/Dockerfile -t agent-wechat-native:v2.2.1-pat .
```

在现有 Compose 的 Native 服务中把 `image` 改成 `agent-wechat-native:v2.2.1-pat`，保留现有配置、凭据及数据卷，然后更新该服务：

```bash
docker compose up -d --no-deps agent-wechat
docker compose logs --tail=200 -f agent-wechat
```

如果构建和部署不在同一台机器，通过已有镜像仓库或 `docker save` / `docker load` 传递镜像。
服务名称按自己的 Compose 调整。回退时恢复原有镜像并重新创建 Native 服务即可。

## 插件设置

更新本插件后开启 `enable_group_pat_reply=true`。群白名单和 `group_reply_mode=disabled` 仍生效；其他模式无需 @。
图片/语音参考需要 `enable_media=true`；语音使用拍人者会话已启用并配置的 AstrBot STT。
每群 30 秒最多处理一次。参考消息可以来自其他成员，但排除机器人自己、拍一拍、已撤回及不支持的消息。
最多查找 20 页，每页 50 条；没有可用上下文或媒体失败时仍交由人格模型回应拍一拍。
`group_reply_style=quote` 对拍一拍回退为直接发送；AstrBot 全局的自动引用装饰也应关闭，因为系统消息不支持原生引用。

## 用户负责的部署调试

- Native 日志 `wechat_pat type=... bytes=... parsed=... actor_self=... target_self=...`：`parsed=true target_self=true actor_self=false` 是需要回应的候选。
- 插件启动日志 `pat_reply=True`；运行日志“拍一拍准入”“拍一拍已交付”“拍一拍上下文不可用”“拍一拍冷却跳过”对应处理阶段。
- 若 Native 有 `parsed=false` 而插件没有拍一拍日志，优先核对实际通知格式；若已交付但未回应，检查模型和 AstrBot 的配置。
- 依次测试他人拍机器人、成员互拍、自己拍自己，并在拍之前分别发文本、图片、语音。

日志不输出凭据、原始 XML、聊天正文、音频或转写正文。

当前已通过 5 个插件针对性测试及补丁应用检查。Native Rust 编译、Docker 构建和真实微信收发未在本机执行，需在部署环境验证。
