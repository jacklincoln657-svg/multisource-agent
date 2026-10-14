"""飞书工作面 @tool。"""
from __future__ import annotations

import json

from langchain_core.tools import tool

from agent.tools_common import (
    SINGLE_EVENT_LIMIT,
    consume_budget,
    dump_raw,
    is_cancelled,
    read_inbox_json,
)
from apitools.feishu import (
    add_plan_rows,
    create_calendar_event,
    create_calendar_events,
    fetch_calendar_events,
    fetch_chat_digest,
    fetch_plan_table,
    feishu_bitable_configured,
    feishu_chat_configured,
    feishu_configured,
)
from context.chat_cache import query_cached_chat as query_cached_chat_engine
from context.chat_pipeline import ingest_and_extract_for_agent


@tool
def fetch_feishu_calendar(days: int = 3) -> str:
    """
    拉取飞书日历近 days 天日程（含今天）。用于「今天忙不忙 / 未来几天有什么会」。
    需要 .env：FEISHU_APP_ID + FEISHU_APP_SECRET；写个人主日历建议再配 FEISHU_USER_ACCESS_TOKEN，
    或固定 FEISHU_CALENDAR_ID（共享日历）。无配置时可用 fetch_mock_feishu_calendar。
    days 默认 3，上限 14。
    """
    if is_cancelled():
        return "Task cancelled; skip Feishu calendar."
    if not feishu_configured():
        return (
            "Feishu not configured. Add to project root .env:\n"
            "  FEISHU_APP_ID=...\n"
            "  FEISHU_APP_SECRET=...\n"
            "  # optional FEISHU_CALENDAR_ID=... or FEISHU_USER_ACCESS_TOKEN=...\n"
            "Open platform: https://open.feishu.cn/app\n"
            "Meanwhile you may call fetch_mock_feishu_calendar."
        )
    try:
        n = max(1, min(int(days or 3), 14))
        payload = fetch_calendar_events(days=n)
        dump_raw("feishu_calendar", payload)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        return f"Feishu calendar fetch failed: {e}"


@tool
def create_feishu_event(
    summary: str,
    start_at: str,
    end_at: str = "",
    description: str = "",
) -> str:
    """
    在飞书日历创建一条日程（需已跑 feishu_oauth_login.py）。
    summary：标题；start_at：开始时间「YYYY-MM-DD HH:MM」；
    end_at：可选，同格式，默认开始后 1 小时；description：备注。
    多条日程必须用 create_feishu_events；本工具同题仅允许 1 次（第 2 次起硬闸拒绝）。
    """
    if is_cancelled():
        return "Task cancelled; skip Feishu event create."
    if not feishu_configured():
        return (
            "Feishu not configured. Add FEISHU_APP_ID / FEISHU_APP_SECRET "
            "and run feishu_oauth_login.py for user calendar write."
        )
    title = (summary or "").strip()
    start = (start_at or "").strip()
    if not title or not start:
        return "summary and start_at are required (start_at like '2026-09-18 15:00')."
    blocked = consume_budget(
        "create_feishu_event",
        SINGLE_EVENT_LIMIT,
        (
            "HARD_STOP: create_feishu_event already used once this turn. "
            "For 2+ events you MUST call create_feishu_events once with a JSON array "
            "(serial writes). Do NOT call create_feishu_event again in parallel — "
            "that races refresh_token and can cause 190007."
        ),
    )
    if blocked:
        return blocked
    try:
        payload = create_calendar_event(
            summary=title,
            start_at=start,
            end_at=(end_at or "").strip() or None,
            description=description or "",
        )
        dump_raw("feishu_event_create", payload)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        return f"Feishu create event failed: {e}"


@tool
def create_feishu_events(events_json: str) -> str:
    """
    一次串行创建多条飞书日程（推荐批量排程用这个，不要并行连调 create_feishu_event）。
    events_json：JSON 数组，每项含 summary、start_at；可选 end_at、description。
    例：[{"summary":"周会","start_at":"2026-09-18 10:00","end_at":"2026-09-18 11:00","description":"同步"}]
    最多约 20 条。返回 created/failed 列表。
    """
    if is_cancelled():
        return "Task cancelled; skip Feishu batch event create."
    if not feishu_configured():
        return (
            "Feishu not configured. Add FEISHU_APP_ID / FEISHU_APP_SECRET "
            "and run feishu_oauth_login.py."
        )
    raw = (events_json or "").strip()
    if not raw:
        return "events_json is required (JSON array)."
    try:
        events = json.loads(raw)
    except json.JSONDecodeError as e:
        return f"events_json is not valid JSON: {e}"
    if not isinstance(events, list):
        return "events_json must be a JSON array."
    try:
        payload = create_calendar_events(events)
        dump_raw("feishu_event_create_batch", payload)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        return f"Feishu batch create failed: {e}"


@tool
def fetch_feishu_plan_table() -> str:
    """
    读取飞书「近日计划」多维表格记录。
    只需 FEISHU_APP_ID / FEISHU_APP_SECRET：若未指定表，会自动创建「阿拉蕾近日计划」
    并把 app_token/table_id 缓存在 workspace/feishu_bitable.json。
    也可在 .env 手动指定 FEISHU_BITABLE_APP_TOKEN / FEISHU_BITABLE_TABLE_ID 覆盖。
    字段约定：日期 / 事项 / 优先级 / 来源 / 状态 / 备注。
    """
    if is_cancelled():
        return "Task cancelled; skip Feishu plan table."
    if not feishu_bitable_configured():
        return (
            "Feishu not configured. Add to project root .env:\n"
            "  FEISHU_APP_ID=...\n"
            "  FEISHU_APP_SECRET=...\n"
            "Plan table IDs are optional (auto-create + local cache).\n"
            "Meanwhile you may call fetch_mock_feishu_plan_table."
        )
    try:
        payload = fetch_plan_table()
        dump_raw("feishu_plan_table", payload)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        return f"Feishu plan table fetch failed: {e}"


@tool
def upsert_feishu_plan_rows(rows_json: str) -> str:
    """
    向飞书近日计划多维表格追加记录（无表则自动创建并缓存）。
    rows_json：JSON 数组字符串，每项至少含「事项」；可选 日期/优先级/来源/状态/备注。
    例：[{"日期":"2026-09-18","事项":"写周报","优先级":"高","来源":"会话","状态":"待办"}]
    一次最多约 20 行。
    【会话卡片】仅当用户已明确确认「写入计划表」后调用；来源建议填「会话」，备注可带 evidence message_id。
    禁止在刚拉完 digest、尚未征得同意时直接写表。
    """
    if is_cancelled():
        return "Task cancelled; skip Feishu plan write."
    if not feishu_bitable_configured():
        return (
            "Feishu not configured. Set FEISHU_APP_ID and FEISHU_APP_SECRET in root .env. "
            "BITABLE_* ids are optional."
        )
    raw = (rows_json or "").strip()
    if not raw:
        return "rows_json is required (JSON array of plan rows)."
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError as e:
        return f"rows_json is not valid JSON: {e}"
    if not isinstance(rows, list):
        return "rows_json must be a JSON array."
    try:
        payload = add_plan_rows(rows)
        dump_raw("feishu_plan_write", payload)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        return f"Feishu plan write failed: {e}"


@tool
def fetch_feishu_chat_digest(
    hours: int = 48,
    max_messages: int = 0,
    recent_n: int = 0,
    chat_id: str = "",
) -> str:
    """
    【白名单会话】拉原文→缓存→旁路提取 info_cards（不含全文）。
    两种模式：
      - recent_n>0：拉最近 N 条（默认建议 50，上限 200），忽略时间窗；适合不活跃群。
      - recent_n=0：按 hours 时间窗（默认 48，上限 168）；max_messages>0 可再软截断。
    追问某句原话用 query_cached_chat；换 hours/recent_n 或要求重拉时再调本工具。
    """
    if is_cancelled():
        return "Task cancelled; skip Feishu chat digest."
    if not feishu_chat_configured():
        return (
            "Feishu chat digest not configured. Add to project root .env:\n"
            "  FEISHU_APP_ID / FEISHU_APP_SECRET\n"
            "  FEISHU_PLAN_CHAT_IDS=oc_xxx,oc_yyy\n"
            "Auth: FEISHU_CHAT_AUTH_MODE=bot (default) or user.\n"
            "Meanwhile you may call fetch_mock_feishu_chat_digest."
        )
    try:
        rn = max(0, int(recent_n or 0))
        payload = fetch_chat_digest(
            hours=max(1, min(int(hours or 48), 168)),
            max_messages=max(0, int(max_messages or 0)),
            recent_n=rn,
            chat_id=(chat_id or "").strip() or None,
        )
        facing = ingest_and_extract_for_agent(payload)
        dump_raw("feishu_chat_digest", {"ingest": payload, "agent_facing": facing})
        return json.dumps(facing, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        return f"Feishu chat digest failed: {e}"


@tool
def fetch_mock_feishu_calendar() -> str:
    """离线 Mock 飞书日历（_inbox/feishu_calendar.json）。无真应用时可练路由。"""
    if is_cancelled():
        return "Task cancelled; skip Feishu calendar."
    return read_inbox_json("feishu_calendar.json")


@tool
def fetch_mock_feishu_plan_table() -> str:
    """离线 Mock 飞书近日计划表（_inbox/feishu_plan_table.json）。"""
    if is_cancelled():
        return "Task cancelled; skip Feishu plan table."
    return read_inbox_json("feishu_plan_table.json")


@tool
def fetch_mock_feishu_chat_digest() -> str:
    """Mock 白名单会话：同真源流水线（缓存 + 旁路提取）；样本 _inbox/feishu_chat_digest.json。"""
    if is_cancelled():
        return "Task cancelled; skip Feishu chat digest."
    raw = read_inbox_json("feishu_chat_digest.json")
    if raw.startswith("数据文件不存在") or raw.startswith("JSON 解析失败"):
        return raw
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return "feishu_chat_digest.json must be a JSON object."
        facing = ingest_and_extract_for_agent(payload)
        return json.dumps(facing, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        return f"Mock Feishu chat digest failed: {e}"


@tool
def query_cached_chat(keyword: str = "", speaker: str = "", limit: int = 10) -> str:
    """
    【白名单会话·检索】从会话缓存按需取原文切片。
    keyword：字面匹配；可用英文/中文逗号一次传多个词（优先照抄 info_cards[].quote_hints）。
    须先 fetch_feishu_chat_digest 或 Mock。禁止不调工具就编造引语。
    """
    if is_cancelled():
        return "Task cancelled; skip cached chat query."
    try:
        payload = query_cached_chat_engine(
            keyword=(keyword or "").strip(),
            speaker=(speaker or "").strip(),
            limit=max(1, min(int(limit or 10), 20)),
        )
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        return f"query_cached_chat failed: {e}"
