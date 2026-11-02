"""Robot configuration. No secrets are included in repr or persisted state."""

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

PLUGIN_ID = "astrbot_plugin_wechat_native"
ADAPTER_ID = "wechat_native"

DEFAULT_CONFIG = {
    "id": "wechat_native",
    "type": ADAPTER_ID,
    "enable": False,
    "server_url": "",
    "account_id": "",
    "token": "",
    "token_file": "",
    "enable_private_reply": True,
    "allowed_users": [],
    "group_reply_mode": "mention",
    "allowed_groups": [],
    "unique_session": True,
    "enable_group_summary": False,
    "summary_max_messages": 200,
    "summary_window_minutes": 60,
}

CONFIG_METADATA = {
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
        "hint": "AstrBot 容器内可读路径；不要填写 Native 宿主机上未挂载的路径",
    },
    "enable_private_reply": {"type": "bool", "description": "启用私聊回复"},
    "allowed_users": {
        "type": "list",
        "description": "私聊用户白名单",
        "hint": "使用 senderId；空列表允许所有私聊用户",
    },
    "group_reply_mode": {
        "type": "string",
        "description": "群聊触发模式",
        "options": ["mention", "all", "disabled"],
        "hint": "mention：明确 @自己；all：所有文本；disabled：关闭",
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
}


@dataclass(frozen=True)
class NativeConfig:
    api_url: str
    account_id: str
    token: str = field(repr=False)
    token_file: str = field(repr=False)
    enable_private_reply: bool = True
    allowed_users: frozenset[str] = frozenset()
    group_reply_mode: str = "mention"
    allowed_groups: frozenset[str] = frozenset()
    unique_session: bool = True
    enable_group_summary: bool = False
    summary_max_messages: int = 200
    summary_window_minutes: int = 60

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
        if mode not in {"mention", "all", "disabled"}:
            raise ValueError("group_reply_mode 必须为 mention/all/disabled")
        for key in ("enable_private_reply", "unique_session", "enable_group_summary"):
            if key in data and not isinstance(data[key], bool):
                raise ValueError(f"{key} 必须是布尔值")
        for key, maximum, default in (
            ("summary_max_messages", 500, 200),
            ("summary_window_minutes", 1440, 60),
        ):
            value = data.get(key, default)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{key} 必须是 1–{maximum} 的整数")
        lists = {}
        for key in ("allowed_users", "allowed_groups"):
            value = data.get(key, [])
            if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
                raise ValueError(f"{key} 必须是非空字符串的列表")
            lists[key] = frozenset(value)
        return cls(
            api_url=url if parsed.path.endswith("/v1") else url + "/v1",
            account_id=account.strip(),
            token=data.get("token", ""),
            token_file=data.get("token_file", ""),
            enable_private_reply=data.get("enable_private_reply", True),
            group_reply_mode=mode,
            unique_session=data.get("unique_session", True),
            enable_group_summary=data.get("enable_group_summary", False),
            summary_max_messages=data.get("summary_max_messages", 200),
            summary_window_minutes=data.get("summary_window_minutes", 60),
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
