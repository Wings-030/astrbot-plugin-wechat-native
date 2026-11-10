"""Robot configuration. No secrets are included in repr or persisted state."""

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

PLUGIN_ID = "astrbot_plugin_wechat_native"
ADAPTER_ID = "wechat_native"

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_VOICE_BYTES = 25 * 1024 * 1024 - 17
MAX_VOICE_SECONDS = 300
VOICE_SEND_TIMEOUT = 25 * 60
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 50
MAX_PDF_TEXT_CHARS = 32000
MEDIA_DOWNLOAD_TIMEOUT = 60
MAX_CONCURRENT_MEDIA_TASKS = 3
DEFAULT_IMAGE_PROMPT = "请描述这张图片的内容。"
DEFAULT_PDF_PROMPT = "请阅读并总结这份 PDF 文件的主要内容。"
RECENT_GROUP_IMAGE_SECONDS = 120
MAX_RECENT_GROUP_IMAGES = 256
RECENT_GROUP_PDF_SECONDS = 120
MAX_RECENT_GROUP_PDFS = 256
RECENT_GROUP_VOICE_SECONDS = 120
MAX_RECENT_GROUP_VOICES = 256

DEFAULT_CONFIG = {
    "id": "wechat_native",
    "type": ADAPTER_ID,
    "enable": False,
    "server_url": "",
    "account_id": "",
    "token": "",
    "token_file": "/run/secrets/wechat-api-token",
    "enable_private_reply": True,
    "enable_media": True,
    "allowed_users": [],
    "group_reply_mode": "mention",
    "group_reply_style": "mention",
    "enable_group_red_packet_reply": False,
    "allowed_groups": [],
    "unique_session": True,
    "enable_group_summary": False,
    "enable_history_tools": False,
    "enable_wechat_actions": False,
    "wechat_action_admins": [],
    "admin_token_file": "/run/secrets/wechat-admin-token",
    "summary_max_messages": 200,
    "summary_window_minutes": 60,
    "enable_offline_notify": False,
    "offline_notify_umo": "",
    "offline_notify_after_seconds": 60,
    "offline_notify_repeat_hours": 6,
    "offline_notify_on_recovery": True,
    "enable_login_qr": False,
    "enable_email_notify": False,
    "email_smtp_host": "",
    "email_smtp_port": 465,
    "email_smtp_security": "ssl",
    "email_smtp_user": "",
    "email_smtp_password": "",
    "email_from": "",
    "email_to": "",
}

CONFIG_METADATA = {
    "enable_wechat_actions": {
        "type": "bool",
        "description": "启用微信修改与撤回工具",
        "hint": (
            "允许 AI 按授权请求修改自己的群昵称、群名或撤回自己发送的消息，无需二次确认；"
            "群改名和撤回仅限管理员白名单，修改群昵称和群名还需配置管理 Token。"
        ),
    },
    "wechat_action_admins": {
        "type": "list",
        "description": "群改名与撤回管理员 senderId 白名单",
        "hint": (
            "填写管理员真实 senderId，例如 wxid_admin，不是昵称或 Native Account ID；"
            "空列表不允许任何人改群名或撤回消息。修改机器人自己的群昵称不受此管理员名单限制，"
            "但仍须符合群聊交互规则。"
        ),
    },
    "admin_token_file": {
        "type": "string",
        "description": "群管理专用 admin Token 文件路径",
        "hint": (
            "默认 /run/secrets/wechat-admin-token；"
            "将宿主机 admin-token 文件只读挂载到此容器内路径，"
            "文件内容须为 Native 签发、具有目标账号 admin 权限的独立 Token。"
            "仅开启微信修改与撤回工具后读取，此处填写文件路径而非 Token 文本。"
        ),
    },
    "enable_history_tools": {
        "type": "bool",
        "description": "启用当前聊天历史与群资料工具",
        "hint": (
            "允许 AI 搜索和读取当前私聊或群聊中 Native 已保存的本地消息，并查询当前群资料与成员；"
            "不跨聊天查询，本地历史可能不完整。群资料和成员查询仅限群聊。"
        ),
    },
    "logo_token": {"type": "string", "invisible": True},
    "server_url": {
        "type": "string",
        "description": "Native 服务地址",
        "hint": "例如 http://native-host:6174，可带 /v1；必须从 AstrBot 容器可达",
    },
    "account_id": {
        "type": "string",
        "description": "Native Account ID",
        "hint": "GET /v1/accounts 返回的 id，不是微信号",
    },
    "token": {
        "type": "string",
        "description": "Native Token",
        "secret": True,
        "hint": "建议留空并使用 token_file；此处填写会存入 AstrBot 配置",
    },
    "token_file": {
        "type": "string",
        "description": "Token 只读文件路径（优先）",
        "hint": (
            "默认 /run/secrets/wechat-api-token，优先于 token；须为 AstrBot 容器内可读文件路径。"
            "不用文件时先清空此项，再填写 token；不要填写未挂载的宿主机路径。"
        ),
    },
    "enable_private_reply": {"type": "bool", "description": "启用私聊回复"},
    "enable_media": {
        "type": "bool",
        "description": "启用图片、PDF 与语音识别",
        "hint": (
            "支持私聊图片、PDF 与语音，语音使用 AstrBot 配置的 ASR；"
            "群聊可引用媒体并 @机器人，或发送后在2分钟内 @机器人解析。"
        ),
    },
    "allowed_users": {
        "type": "list",
        "description": "私聊用户白名单",
        "hint": "使用 senderId；空列表允许所有私聊用户",
    },
    "group_reply_mode": {
        "type": "string",
        "description": "群聊触发模式",
        "options": ["mention", "all", "probability", "disabled"],
        "labels": ["仅 @", "全部回复", "概率回复", "关闭"],
        "hint": (
            "mention：明确 @自己；all：所有文本与直接语音都直接唤醒；"
            "probability：所有文本与直接语音都进入 AstrBot，但只有 @ 才唤醒，"
            "其余交给 AstrBot 的主动回复概率；disabled：关闭"
        ),
    },
    "group_reply_style": {
        "type": "string",
        "description": "群聊回复方式",
        "options": ["mention", "quote", "plain"],
        "labels": ["@触发者", "引用触发消息", "直接发送"],
        "hint": (
            "首段回复自动 @或引用，也可直接发送；纯图片/语音直接发送。"
            "插件显式添加的 @和引用仍保留。"
        ),
    },
    "enable_group_red_packet_reply": {
        "type": "bool",
        "description": "群红包自动暖场",
        "hint": "在允许回复的群中，收到真实微信红包时主动回一句，同群 30 秒最多一次。",
    },
    "allowed_groups": {
        "type": "list",
        "description": "群白名单",
        "hint": "使用 threadId（通常以 @chatroom 结尾）；空列表允许所有群",
    },
    "unique_session": {
        "type": "bool",
        "description": "群内成员独立会话",
        "hint": "关闭后同一个群共享会话；不同账号/聊天始终隔离",
    },
    "enable_group_summary": {
        "type": "bool",
        "description": "启用群聊摘要",
        "hint": "内存缓存允许群的实时文本；群内真正 @机器人发送“总结群聊”，重启清空",
    },
    "summary_max_messages": {
        "type": "int",
        "description": "每群摘要缓存条数",
        "hint": "1–500，默认 200；所有群合计最多 2000 条",
    },
    "summary_window_minutes": {
        "type": "int",
        "description": "摘要消息时间范围（分钟）",
        "hint": "1–1440，默认 60；只包含开启功能后收到的实时文本",
    },
    "enable_offline_notify": {
        "type": "bool",
        "description": "掉线提醒",
        "hint": (
            "个人微信掉线或 Native 接口不可达时，通过本机已启用的 weixin_oc 机器人私聊提醒；"
            "恢复在线时也会通知。通知对象自动发现，无需填写用户 ID。"
        ),
    },
    "offline_notify_umo": {
        "type": "string",
        "description": "提醒发送目标（留空自动发现）",
        "hint": (
            "格式如 weixin_personal_wpic:FriendMessage:用户ID；"
            "留空时自动使用最近联系的 weixin_oc 机器人会话。"
        ),
    },
    "offline_notify_after_seconds": {
        "type": "int",
        "description": "持续掉线多久才提醒（秒）",
        "hint": "10–3600，默认 60；连续检测失败达到该时长才发送，避免网络抖动误报",
    },
    "offline_notify_repeat_hours": {
        "type": "int",
        "description": "持续掉线的补提醒间隔（小时）",
        "hint": "0–168，默认 6；填 0 表示掉线期间只提醒一次",
    },
    "offline_notify_on_recovery": {
        "type": "bool",
        "description": "恢复在线时提醒",
        "hint": "关闭后只在掉线时发送提醒",
    },
    "enable_login_qr": {
        "type": "bool",
        "description": "掉线时自动发送登录二维码",
        "hint": (
            "判定掉线后通过已配置的 weixin_oc 目标发送微信登录二维码，直接用手机扫码即可；"
            "二维码约 4 分钟过期，过期后在聊天里发送“微信二维码”可重新获取。"
        ),
    },
    "enable_email_notify": {
        "type": "bool",
        "description": "邮件提醒",
        "hint": (
            "掉线时额外发送邮件，与微信提醒互不影响：两个都开就同时发，一个失败不影响另一个。"
            "邮件通道没有会话窗口限制。"
        ),
    },
    "email_smtp_host": {
        "type": "string",
        "description": "SMTP 服务器地址",
        "hint": "例如 smtp.qq.com、smtp.163.com、smtp.gmail.com",
    },
    "email_smtp_port": {
        "type": "int",
        "description": "SMTP 端口",
        "hint": "SSL 通常 465，STARTTLS 通常 587",
    },
    "email_smtp_security": {
        "type": "string",
        "description": "SMTP 加密方式",
        "options": ["ssl", "starttls", "none"],
        "labels": ["SSL", "STARTTLS", "不加密"],
        "hint": "按邮箱服务商要求选择；一般选 SSL",
    },
    "email_smtp_user": {
        "type": "string",
        "description": "SMTP 登录账号",
        "hint": "通常是完整邮箱地址，例如 you@qq.com",
    },
    "email_smtp_password": {
        "type": "string",
        "description": "SMTP 授权码",
        "secret": True,
        "hint": "不是邮箱登录密码；QQ/163 需要在网页版开启 SMTP 服务后生成授权码",
    },
    "email_from": {
        "type": "string",
        "description": "发件人地址（留空使用 SMTP 账号）",
    },
    "email_to": {
        "type": "string",
        "description": "收件人地址",
        "hint": "可以填自己的邮箱；多人用英文逗号分隔",
    },
}


@dataclass(frozen=True)
class NativeConfig:
    api_url: str
    account_id: str
    token: str = field(repr=False)
    token_file: str = field(repr=False)
    enable_private_reply: bool = True
    enable_media: bool = True
    allowed_users: frozenset[str] = frozenset()
    group_reply_mode: str = "mention"
    group_reply_style: str = "mention"
    enable_group_red_packet_reply: bool = False
    allowed_groups: frozenset[str] = frozenset()
    unique_session: bool = True
    enable_group_summary: bool = False
    enable_history_tools: bool = False
    enable_wechat_actions: bool = False
    wechat_action_admins: frozenset[str] = frozenset()
    admin_token_file: str = field(default="", repr=False)
    summary_max_messages: int = 200
    summary_window_minutes: int = 60
    enable_offline_notify: bool = False
    offline_notify_umo: str = ""
    offline_notify_after_seconds: int = 60
    offline_notify_repeat_hours: int = 6
    offline_notify_on_recovery: bool = True
    enable_login_qr: bool = False
    enable_email_notify: bool = False
    email_smtp_host: str = ""
    email_smtp_port: int = 465
    email_smtp_security: str = "ssl"
    email_smtp_user: str = ""
    email_smtp_password: str = field(default="", repr=False)
    email_from: str = ""
    email_to: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "NativeConfig":
        url = str(data.get("server_url", "")).strip().rstrip("/")
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("server_url 必须是无认证信息、查询参数或片段的 HTTP(S) 地址")
        try:
            parsed.port
        except ValueError:
            raise ValueError("server_url 端口无效") from None
        account = data.get("account_id", "")
        if not isinstance(account, str) or not account.strip():
            raise ValueError("account_id 必填")
        mode = data.get("group_reply_mode", "mention")
        if mode not in {"mention", "all", "probability", "disabled"}:
            raise ValueError("group_reply_mode 必须为 mention/all/probability/disabled")
        style = data.get("group_reply_style", "mention")
        if not isinstance(style, str) or style not in {"mention", "quote", "plain"}:
            raise ValueError("group_reply_style 必须为 mention/quote/plain")
        for key in (
            "enable_private_reply",
            "enable_media",
            "enable_group_red_packet_reply",
            "unique_session",
            "enable_group_summary",
            "enable_history_tools",
            "enable_wechat_actions",
            "enable_offline_notify",
            "offline_notify_on_recovery",
            "enable_email_notify",
            "enable_login_qr",
        ):
            if key in data and not isinstance(data[key], bool):
                raise ValueError(f"{key} 必须是布尔值")
        for key, maximum, default in (
            ("summary_max_messages", 500, 200),
            ("summary_window_minutes", 1440, 60),
        ):
            value = data.get(key, default)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{key} 必须是 1–{maximum} 的整数")
        after_seconds = data.get("offline_notify_after_seconds", 60)
        if type(after_seconds) is not int or not 10 <= after_seconds <= 3600:
            raise ValueError("offline_notify_after_seconds 必须是 10–3600 的整数")
        repeat_hours = data.get("offline_notify_repeat_hours", 6)
        if type(repeat_hours) is not int or not 0 <= repeat_hours <= 168:
            raise ValueError("offline_notify_repeat_hours 必须是 0–168 的整数")
        notify_umo = data.get("offline_notify_umo", "")
        if not isinstance(notify_umo, str):
            raise ValueError("offline_notify_umo 必须是字符串")
        email_port = data.get("email_smtp_port", 465)
        if type(email_port) is not int or not 1 <= email_port <= 65535:
            raise ValueError("email_smtp_port 必须是 1–65535 的整数")
        email_security = data.get("email_smtp_security", "ssl")
        if email_security not in {"ssl", "starttls", "none"}:
            raise ValueError("email_smtp_security 必须为 ssl/starttls/none")
        for key in (
            "email_smtp_host",
            "email_smtp_user",
            "email_smtp_password",
            "email_from",
            "email_to",
        ):
            if not isinstance(data.get(key, ""), str):
                raise ValueError(f"{key} 必须是字符串")
        lists = {}
        for key in ("allowed_users", "allowed_groups", "wechat_action_admins"):
            value = data.get(key, [])
            if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
                raise ValueError(f"{key} 必须是非空字符串的列表")
            lists[key] = frozenset(value)
        if not isinstance(data.get("admin_token_file", ""), str):
            raise ValueError("admin_token_file 必须是文件路径字符串")
        return cls(
            api_url=url if parsed.path.endswith("/v1") else url + "/v1",
            account_id=account.strip(),
            token=data.get("token", ""),
            token_file=data.get("token_file", ""),
            enable_private_reply=data.get("enable_private_reply", True),
            enable_media=data.get("enable_media", True),
            group_reply_mode=mode,
            group_reply_style=style,
            enable_group_red_packet_reply=data.get("enable_group_red_packet_reply", False),
            unique_session=data.get("unique_session", True),
            enable_group_summary=data.get("enable_group_summary", False),
            enable_history_tools=data.get("enable_history_tools", False),
            enable_wechat_actions=data.get("enable_wechat_actions", False),
            admin_token_file=data.get("admin_token_file", ""),
            summary_max_messages=data.get("summary_max_messages", 200),
            summary_window_minutes=data.get("summary_window_minutes", 60),
            enable_offline_notify=data.get("enable_offline_notify", False),
            offline_notify_umo=notify_umo.strip(),
            offline_notify_after_seconds=after_seconds,
            offline_notify_repeat_hours=repeat_hours,
            offline_notify_on_recovery=data.get("offline_notify_on_recovery", True),
            enable_login_qr=data.get("enable_login_qr", False),
            enable_email_notify=data.get("enable_email_notify", False),
            email_smtp_host=data.get("email_smtp_host", "").strip(),
            email_smtp_port=email_port,
            email_smtp_security=email_security,
            email_smtp_user=data.get("email_smtp_user", "").strip(),
            email_smtp_password=data.get("email_smtp_password", ""),
            email_from=data.get("email_from", "").strip(),
            email_to=data.get("email_to", "").strip(),
            **lists,
        )

    def load_token(self) -> str:
        token = self.token
        if self.token_file:
            try:
                with Path(self.token_file).open() as f:
                    token = f.read(8193).rstrip("\r\n")
            except (OSError, TypeError, ValueError):
                raise ValueError("token_file 不可读，请检查容器挂载与权限") from None
        if (
            not isinstance(token, str)
            or not token
            or len(token) > 8192
            or any(ord(c) < 33 or ord(c) > 126 for c in token)
        ):
            raise ValueError("必须配置有效的 token 或 token_file（不含空格或控制字符）")
        return token
