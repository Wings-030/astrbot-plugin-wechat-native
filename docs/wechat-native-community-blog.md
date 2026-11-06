# 把 AstrBot 接入个人微信：一个基于 Native API 的插件实践

如果已经在用 AstrBot 管理模型和插件，想在微信里继续使用，通常还需要解决一层接入问题：微信消息怎么进入 AstrBot，AstrBot 生成的文本、图片和语音又怎么回到微信？

[WeChat Native](https://github.com/neozhu/astrbot-plugin-wechat-native) 做的就是这层适配。它作为 AstrBot 插件运行，通过 [agent-wechat-native](https://github.com/thisnick/agent-wechat) 的 `/v1` API 连接个人微信，不需要再单独部署一个 Bridge 服务。Native 本身仍然需要运行并登录微信，AstrBot 也需要配置可用的模型。

这篇文章聊聊它的接入方式，以及比“收到消息、调用模型、发回结果”更容易被忽略的几个细节。

## 消息经过哪些环节？

从左到右，消息依次经过微信客户端、agent-wechat-native、WeChat Native 插件和 AstrBot；回复沿相反方向返回。Native 与插件通过 `/v1` API 通信：

![微信消息接入 AstrBot 的链路](wechat-native-diagram.png)

职责划分比较直接：Native 负责微信侧的连接和媒体能力；插件把消息转换成 AstrBot 能处理的事件，再把 AstrBot 的回复交给 Native；模型、人格和语音服务仍在 AstrBot 中配置。

目前插件适配 AstrBot `>=4.29.3,<5`、Native `v1.4.0`，运行需要 Python 3.12+。除了文本私聊，也支持群聊回复、图片识别、PDF 解析、微信语音收发、群聊摘要和引用回复。普通文件发送暂不支持。

## 从安装到第一条回复

先启动 Native 并完成微信登录，记下 `wx accounts list` 返回的账号 ID。然后在 AstrBot 的「插件管理 → 从链接安装」中填入：

```text
https://github.com/neozhu/astrbot-plugin-wechat-native
```

安装后，在「机器人 → 新增机器人」中选择 **WeChat Native**，填写 Native 地址、账号 ID 和 Token。使用 Token 文件时，`token_file` 要填 AstrBot 进程实际能读到的路径；如果 AstrBot 在容器里，这通常是**容器内路径**。Token 需要目标账号的 `read`、`send`、`media` 权限。

![WeChat Native 机器人参数配置](https://raw.githubusercontent.com/neozhu/astrbot-plugin-wechat-native/main/docs/wechat-nactive.png)

接着给机器人选择 AstrBot 配置文件，设置模型和人格。插件的可视化视图里有连接测试：API 显示已连接，只说明 Native API 验证通过；微信账号在线状态是另一项信息，模型和消息发送是否正常还需要实际发消息测试。

仓库 README 给出了同一台 Linux 主机上使用 Docker Compose 部署 AstrBot 和 Native 的完整例子。两个容器使用 host 网络时，AstrBot 可以通过 `http://127.0.0.1:6174` 访问 Native。如果改成 bridge 网络，容器里的 `127.0.0.1` 指向容器自身，不能照搬这个地址。

## 群聊里，什么时候应该回应？

私聊默认开启。群聊默认使用 `mention` 模式：只有真正 @机器人时才回复，手动输入 `@名字` 或只 @全体不算。也可以选择处理全部群文本、交给 AstrBot 的主动回复概率，或者关闭群回复。

这里有两个容易混淆的设置：**触发规则**决定机器人什么时候处理消息；**回复方式**决定回复时 @触发者、引用原消息还是直接发送。两者可以分别配置。

插件还提供私聊用户和群的白名单。需要留意，`allowed_users` 的空列表表示允许所有私聊用户，`allowed_groups` 的空列表表示允许所有群；前者也不限制群成员。准备长期运行时，建议先按自己的使用范围填写，再检查 AstrBot 自身的全局白名单和插件权限。

默认开启的 `unique_session` 会让同一群内不同成员使用独立会话；关闭后，同群成员共享会话。选哪种取决于你希望机器人记住个人上下文，还是让它参与群体讨论。

## 图片和语音：接入不等于模型已经能理解

收到私聊图片后，插件可以通过 Native Media API 拉取图片，交给 AstrBot；要让机器人回答图片内容，仍需配置支持视觉的模型。群里则需要真正 @机器人：可以引用图片提问，也可以在发图后两分钟内由同一成员 @机器人请求分析。普通群图片不会因此立刻下载。

语音也是类似的分工。接收微信语音时，Native 将音频转换为 WAV，插件交付 AstrBot 的标准 `Record`，之后由 AstrBot 配置的语音识别服务处理。需要语音回复时，还要配置文字转语音服务。没有配置 ASR 时，插件不会替你选择服务商，也不会凭空生成转写结果。

发送图片或语音时，插件先上传媒体，再提交发送任务。文本、图片和语音组成的消息链按顺序处理；Native 对图片和语音的请求不承载引用或 @，需要这些信息时，应让非空文本承担。纯图片或纯语音回复不会自动 @。

## 为什么发送回执要持久化？

消息发送有一种麻烦情况：请求已经发出，连接却在收到结果前断了。此时客户端无法仅凭超时判断对方到底有没有收到。如果直接重新发送，就可能产生重复消息。

插件把发送任务和回执保存在 SQLite 中。图片提交结果不明时，会使用原幂等键查询，之后只查询原任务，而不是换一个键重新上传和发送。长语音还会记录分段回执；如果只完成了一部分，状态会标记为 `partial`，不确定的任务会保留 `needs_review`，供人工检查。

这意味着遇到不确定状态时，先查看实际聊天和状态记录，再决定怎么处理。Native 返回 `accepted` 也不等于已经在收件端成功播放，语音和原生引用的实际显示效果仍要在微信里验证。

## 群聊摘要的取舍

开启 `enable_group_summary` 后，插件会在内存里缓存允许的群的实时文本。默认群聊模式下，真正 @机器人并发送「总结群聊」或「群聊摘要」，就会把选中的记录交给当前 AstrBot 会话的模型整理。普通群消息进入摘要缓存，并不会因此触发回复。

缓存只包含功能开启后收到的实时文本，不补取历史，也不包含图片和语音；重启或停止机器人后清空。默认每群最多缓存 200 条、时间窗口为 60 分钟。触发摘要时，选中的群聊内容会发送给当前配置的模型服务，因此启用前值得确认模型服务的位置和群成员对数据处理的预期。

## 最后

这个插件的重点是把微信侧能力接到 AstrBot 已有的会话和模型体系中。快速试用时，先跑通 Native 登录、API 连接、模型配置和一条私聊，再逐步打开群聊、媒体和摘要，会比较容易定位问题。

项目地址：[neozhu/astrbot-plugin-wechat-native](https://github.com/neozhu/astrbot-plugin-wechat-native)。完整部署步骤、配置项和限制以仓库 README 为准。请仅接入自己有权操作的微信账号。
