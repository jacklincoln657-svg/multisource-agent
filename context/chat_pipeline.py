"""
白名单会话流水线（整理后的单一入口）：

  拉群/Mock payload
       → store_chat_digest（原文进会话缓存）
       → extract_chat_info_cards（旁路专用 Prompt）
       → 返回给主 Agent：info_cards + 索引（不含 messages 全文）

追问原话：query_cached_chat（读缓存，不滚雪球）。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from context.chat_extract import extract_chat_info_cards
from context.chat_cache import (
    cache_index,
    get_cached_messages,
    store_chat_digest,
    store_info_cards,
)


def ingest_and_extract_for_agent(
    payload: dict[str, Any],
    *,
    llm_factory: Optional[Callable[[], Any]] = None,
) -> dict[str, Any]:
    """
    写入缓存并旁路提取。返回主 Agent 可见结构（无原始 messages）。
    llm_factory：默认懒加载 agent.core.get_llm，避免循环 import。
    """
    if not isinstance(payload, dict):
        return {"ok": False, "error": "invalid_payload", "info_cards": []}

    index = store_chat_digest(payload)
    messages = get_cached_messages()
    meta = {
        "hours": payload.get("hours"),
        "range_start": payload.get("range_start"),
        "range_end": payload.get("range_end"),
        "max_messages": payload.get("max_messages"),
        "ingest_policy": payload.get("ingest_policy"),
    }

    extract_result: dict[str, Any] = {
        "ok": False,
        "cards": [],
        "card_count": 0,
    }
    try:
        if llm_factory is None:
            from agent.core import get_llm

            llm_factory = get_llm
        llm = llm_factory()
        extract_result = extract_chat_info_cards(messages, llm=llm, meta=meta)
        store_info_cards(
            extract_result.get("cards") or [],
            extract_meta={
                "chunks_used": extract_result.get("chunks_used"),
                "raw_message_count": extract_result.get("raw_message_count"),
                "detail_policy": extract_result.get("detail_policy"),
            },
        )
        index = cache_index()
    except Exception as e:
        extract_result = {
            "ok": False,
            "cards": [],
            "card_count": 0,
            "error": str(e)[:400],
        }

    slim_chats = []
    for c in payload.get("chats") or []:
        if not isinstance(c, dict):
            continue
        slim_chats.append(
            {
                "chat_id": c.get("chat_id"),
                "message_count": c.get("message_count"),
                "truncated": bool(c.get("truncated")),
                "messages_omitted": True,
            }
        )

    return {
        "ok": True,
        "source": payload.get("source") or "feishu_chat_digest",
        "hours": payload.get("hours"),
        "range_start": payload.get("range_start"),
        "range_end": payload.get("range_end"),
        "whitelist": payload.get("whitelist"),
        "chats": slim_chats,
        "session_cache": index,
        "info_cards": extract_result.get("cards") or [],
        "info_card_count": int(extract_result.get("card_count") or 0),
        "extract_ok": bool(extract_result.get("ok")),
        "extract_error": extract_result.get("error"),
        "raw_message_count": extract_result.get("raw_message_count")
        or index.get("message_count"),
        "note": (
            "原文仅在会话缓存。请用 info_cards 向用户说明（有效细节已写够）；"
            "确认后再写表/日历；追问原话用 query_cached_chat，"
            "keyword 优先照抄卡上 quote_hints（逗号拼接）。"
        ),
    }
