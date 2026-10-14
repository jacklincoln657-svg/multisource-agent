"""网页证据 @tool。"""
from __future__ import annotations

import json
import os
from datetime import datetime

from langchain_core.tools import tool

from agent.tools_common import (
    RAW_DIR,
    WEB_FETCH_LIMIT,
    WEB_SEARCH_LIMIT,
    consume_budget,
    is_cancelled,
)
from apitools.web import (
    slim_evidence_payload,
    web_fetch as web_fetch_page,
    web_search as web_search_engine,
    web_search_configured,
)


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """
    使用网页搜索（默认 Tavily，面向 Agent）返回精简「证据卡」列表（title/url/snippet）。

    【触发边界——极其重要】平台/本地工具能完整回答的，一律先用它们，不准向外搜。
    已注册工具：Steam、B 站私人轨迹、Spotify Mock 等。

    适合 web_search：
    - 版本对照、补丁、新闻、行业动态、需官网/社区核实的外部机制；
    - 【证据升级】用户要剧情/「讲了什么」/设定详解，而 B 站/Steam 只给出了标题或短简介时——必须再搜，不算乱搜。

    禁止：用网页代替已有源去查「我最近用了什么 / 库或听歌 / 平台内价评 /
    已由 search_* / lookup_* / fetch_* 能完整回答的详情」。

    流程：按主题必要搜索；摘要不够再 web_fetch（合计最多约 2 URL）。
    【止损·硬闸】同一用户问题内 web_search 最多 2 次（代码计数，超限直接拒绝）；仍不够则停止再搜。
    禁止无新信息的同义反复搜索。需要根目录 .env：TAVILY_API_KEY（https://app.tavily.com/home，免绑卡）。
    """
    if is_cancelled():
        return "Task cancelled; skip web search."
    q = (query or "").strip()
    if not q:
        return "query is required."
    blocked = consume_budget(
        "web_search",
        WEB_SEARCH_LIMIT,
        (
            "HARD_STOP: web_search budget exhausted for this user question "
            f"(limit={WEB_SEARCH_LIMIT}). Do NOT search again. "
            "Admit external evidence is insufficient; answer from cards already obtained "
            "and mark uncertainty. Never synonym-retry."
        ),
    )
    if blocked:
        return blocked
    if not web_search_configured():
        return (
            "Web search not configured. Add to project root .env:\n"
            "  TAVILY_API_KEY=...\n"
            "Get a key (no credit card): https://app.tavily.com/home"
        )
    try:
        n = max(3, min(int(max_results or 5), 8))
        payload = web_search_engine(q, max_results=n)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        full = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with open(os.path.join(RAW_DIR, f"web_search_{stamp}.json"), "w", encoding="utf-8") as f:
            f.write(full)
        with open(os.path.join(RAW_DIR, "web_search_latest.json"), "w", encoding="utf-8") as f:
            f.write(full)
        slim = slim_evidence_payload(payload.get("cards") or [], query=q)
        return json.dumps(slim, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        return f"web_search failed: {e}"


@tool
def web_fetch(url: str) -> str:
    """
    拉取单个网页并截断正文，返回一张证据卡（page_excerpt）。
    仅在 web_search 之后、且摘要不足以回答时使用；同一问题最多 2 URL（代码硬闸）。
    禁止用来替代任何已注册的平台 fetch/search/lookup 工具；禁止抓取 localhost/内网。
    可选 .env WEB_FETCH_ALLOWLIST=domain1.com,domain2.com（设置后仅允许这些域名）。
    """
    if is_cancelled():
        return "Task cancelled; skip web fetch."
    u = (url or "").strip()
    if not u:
        return "url is required (from a prior web_search card)."
    blocked = consume_budget(
        "web_fetch",
        WEB_FETCH_LIMIT,
        (
            "HARD_STOP: web_fetch budget exhausted for this user question "
            f"(limit={WEB_FETCH_LIMIT} URLs). Do NOT fetch more pages. "
            "Stop and answer from existing evidence; mark uncertainty if needed."
        ),
    )
    if blocked:
        return blocked
    try:
        payload = web_fetch_page(u)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        full = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with open(os.path.join(RAW_DIR, f"web_fetch_{stamp}.json"), "w", encoding="utf-8") as f:
            f.write(full)
        with open(os.path.join(RAW_DIR, "web_fetch_latest.json"), "w", encoding="utf-8") as f:
            f.write(full)
        slim = slim_evidence_payload(payload.get("cards") or [], query=u)
        return json.dumps(slim, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        return f"web_fetch failed: {e}"
