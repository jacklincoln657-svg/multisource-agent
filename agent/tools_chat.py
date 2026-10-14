"""本站对话归档工具（固定通用能力，非数据源插件）。"""
from __future__ import annotations

import json

from langchain_core.tools import tool

from agent.tools_common import WORKSPACE_DIR, is_cancelled
from context.dialogue import archived_session_for_agent, list_archived_sessions


@tool
def list_chat_archives(limit: int = 12) -> str:
    """
    【本站对话归档】列出左侧「历史会话」已保存的归档（主题/时间/文件名）。
    用户问「之前聊过什么 / 上次那个飞书/Steam」且当前是新会话时先调这个；
    再 read_chat_archive(filename 或 topic_query)。不是飞书群聊。
    """
    if is_cancelled():
        return "Task cancelled; skip list archives."
    try:
        n = max(1, min(int(limit or 12), 40))
        items = list_archived_sessions(WORKSPACE_DIR, limit=n)
        slim = [
            {
                "filename": s["filename"],
                "topic": s.get("topic") or "未命名",
                "archived_at": s.get("archived_at") or "",
                "message_count": s.get("message_count") or 0,
                "has_summary": bool(s.get("has_summary")),
            }
            for s in items
        ]
        return json.dumps(
            {
                "source": "chat_archives",
                "count": len(slim),
                "archives": slim,
                "note": "只读索引。详情用 read_chat_archive；勿与飞书白名单群混淆。",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except Exception as e:
        return f"list_chat_archives failed: {e}"


@tool
def read_chat_archive(
    filename: str = "",
    topic_query: str = "",
    max_messages: int = 24,
) -> str:
    """
    【本站对话归档】读取一条已保存的历史会话（可见对话+摘要，无 tool 轨迹）。
    filename 来自 list_chat_archives；或用 topic_query 主题关键字匹配最近一条。
    只读，不会自动并入当前对话。用户要继续旧话题可复述要点，或请其点左侧历史打开。
    """
    if is_cancelled():
        return "Task cancelled; skip read archive."
    try:
        payload = archived_session_for_agent(
            WORKSPACE_DIR,
            filename=(filename or "").strip(),
            topic_query=(topic_query or "").strip(),
            max_messages=max(4, min(int(max_messages or 24), 60)),
        )
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        return f"read_chat_archive failed: {e}"
