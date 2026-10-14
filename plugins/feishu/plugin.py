"""飞书工作面插件（日历 / 计划表 / 白名单群聊）。"""
from __future__ import annotations

from pathlib import Path

from plugins.feishu.tools import (
    create_feishu_event,
    create_feishu_events,
    fetch_feishu_calendar,
    fetch_feishu_chat_digest,
    fetch_feishu_plan_table,
    fetch_mock_feishu_calendar,
    fetch_mock_feishu_chat_digest,
    fetch_mock_feishu_plan_table,
    query_cached_chat,
    upsert_feishu_plan_rows,
)
from apitools.feishu import (
    feishu_auth_health,
    feishu_chat_configured,
    feishu_configured,
)

PLUGIN_ID = "feishu"
TITLE = "飞书工作面"
TOOLS = [
    fetch_feishu_calendar,
    create_feishu_event,
    create_feishu_events,
    fetch_feishu_plan_table,
    upsert_feishu_plan_rows,
    fetch_feishu_chat_digest,
    fetch_mock_feishu_calendar,
    fetch_mock_feishu_plan_table,
    fetch_mock_feishu_chat_digest,
    query_cached_chat,
]
SKILL_FILE = Path(__file__).with_name("SKILL.md")


def status_line() -> str:
    if not feishu_configured():
        return (
            "未配置飞书：请在根目录 .env 填写 FEISHU_APP_ID / FEISHU_APP_SECRET；"
            "此前可用 fetch_mock_feishu_*"
        )
    health = feishu_auth_health()
    issues = health.get("issues") or []
    parts = [
        "已配置飞书应用（日历可用；近日计划表可自动创建并缓存到 workspace/feishu_bitable.json）"
    ]
    if feishu_chat_configured():
        parts.append("白名单会话可用")
    else:
        parts.append("会话摘要未配 PLAN_CHAT_IDS")
    if health.get("ok"):
        parts.append("OAuth/scope 就绪")
    elif issues:
        parts.append("OAuth 待修: " + "；".join(str(x) for x in issues[:2]))
    else:
        parts.append("OAuth 未就绪")
    return "；".join(parts)
