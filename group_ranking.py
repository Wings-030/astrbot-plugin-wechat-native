"""Bounded, request-local statistics over today's saved group texts."""

import asyncio
import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from astrbot.api import logger

from .event import message_timestamp

COMMAND = "谁最能聊"
EMPTY_MESSAGE = "今天暂无可统计的聊天消息。"
QUERY_ERROR = "暂时查不到群聊记录，请稍后再试。"


@dataclass
class DailyRanking:
    leaders: list[tuple[str, int]]
    names: dict[str, str]
    texts: list[str]
    partial: bool
    bot_name: str = "机器人"


def is_ranking_command(raw):
    mentions = raw.get("mentions")
    if not (
        raw.get("type") == 1
        and isinstance(raw.get("body"), str)
        and isinstance(mentions, dict)
        and mentions.get("state") == "known"
        and mentions.get("self") is True
    ):
        return False
    text = re.sub(r"^(?:@[^\u2005\r\n]+\u2005[ \t]*)+", "", raw["body"])
    return text.strip() == COMMAND


def short_id(sender):
    if len(sender) <= 20:
        return sender
    return sender[:12] + "…" + hashlib.sha256(sender.encode()).hexdigest()[:6]


async def member_name(client, account, thread, username):
    try:
        async with asyncio.timeout(3):
            value = await client.group_member(thread, username)
        member = value.get("member")
        if (
            value.get("accountId", account) != account
            or value.get("groupId") != thread
            or value.get("username") != username
            or value.get("state") != "present"
            or not isinstance(member, dict)
            or member.get("username") != username
            or member.get("groupId", thread) != thread
            or member.get("accountId", account) != account
        ):
            return ""
        for key in ("roomDisplayName", "displayName"):
            name = member.get(key)
            if isinstance(name, str) and name.strip():
                return " ".join(name.split())[:40]
    except Exception as error:
        logger.warning("WeChat Native 排行榜成员名称查询失败：type=%s", type(error).__name__)
    return ""


async def collect_ranking(ctx):
    now = datetime.now().astimezone()
    start, end = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp(), now.timestamp()
    counts, seen, texts = Counter(), set(), []
    cursor, cursors, pages, partial = "", set(), 0, False
    client, bot = ctx.adapter.client, ctx.adapter.bot_self_id
    try:
        async with asyncio.timeout(15):
            for _ in range(20):
                page = await client.messages(ctx.thread, limit=50, cursor=cursor)
                items = page.get("items")
                if not isinstance(items, list) or len(items) > 50:
                    raise ValueError("invalid_history_page")
                for item in items:
                    if not isinstance(item, dict):
                        raise ValueError("invalid_history_item")
                    if item.get("deleted") is True:
                        continue
                    if item.get("accountId") != ctx.account or item.get("threadId") != ctx.thread:
                        raise ValueError("history_scope_mismatch")
                    mid, sender, body = item.get("id"), item.get("senderId"), item.get("body")
                    stamp = message_timestamp(item)
                    if (
                        not isinstance(mid, str)
                        or not mid
                        or mid in seen
                        or mid == ctx.message_id
                        or not isinstance(sender, str)
                        or not sender
                        or sender == bot
                        or item.get("type") != 1
                        or item.get("direction") != "incoming"
                        or item.get("recall") is not None
                        or not isinstance(body, str)
                        or not body.strip()
                        or stamp is None
                        or not start <= stamp <= end
                        or is_ranking_command(item)
                    ):
                        continue
                    seen.add(mid)
                    counts[sender] += 1
                    texts.append((stamp, mid, body[:200]))
                pages += 1
                cursor = page.get("nextCursor")
                if cursor is None:
                    break
                if not isinstance(cursor, str) or not cursor or cursor in cursors:
                    partial = True
                    break
                cursors.add(cursor)
            else:
                partial = True
    except Exception as error:
        logger.warning("WeChat Native 排行榜历史查询失败：type=%s", type(error).__name__)
        if not pages:
            return None
        partial = True
    leaders = sorted(counts.items(), key=lambda row: (-row[1], row[0]))[:5]
    names = {}
    if leaders:
        senders = [sender for sender, _ in leaders]
        resolved = await asyncio.gather(
            *(member_name(client, ctx.account, ctx.thread, sender) for sender in senders)
        )
        names = {sender: name or short_id(sender) for sender, name in zip(senders, resolved)}
        duplicates = Counter(names.values())
        names = {
            sender: f"{name}（{short_id(sender)}）" if duplicates[name] > 1 else name
            for sender, name in names.items()
        }
    return DailyRanking(leaders, names, [text for _, _, text in sorted(texts)], partial)


def format_ranking(ranking):
    if ranking is None:
        return QUERY_ERROR
    if not ranking.leaders:
        return EMPTY_MESSAGE
    lines = [COMMAND, ""]
    lines.extend(
        f"{index}. {ranking.names[sender]}：{count} 条"
        for index, (sender, count) in enumerate(ranking.leaders, 1)
    )
    if ranking.partial:
        lines.extend(["", "本次为部分统计。"])
    return "\n".join(lines)
