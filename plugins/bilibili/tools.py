"""Bilibili 插件 @tool。"""
from __future__ import annotations

import json
import os
from datetime import datetime

from langchain_core.tools import tool

from agent.tools_common import RAW_DIR, is_cancelled, read_inbox_json
from apitools.bilibili import (
    bilibili_configured,
    fetch_bilibili_followings_bundle,
    fetch_bilibili_private_bundle,
    lookup_up_bundle,
    slim_bilibili_for_agent,
)


@tool
def fetch_bilibili_data(history_n: int = 30) -> str:
    """
    拉取私人 B 站「轻量画像」：登录资料 + 观看历史（标题/分区/进度等元数据）。
    需要 .env 的 BILI_COOKIE（须含 SESSDATA；勿外泄/勿提交 Git）。
    返回精简 JSON：profile / history[]；完整原文落盘 workspace/raw/。

    history_n：默认 30（省 token）。仅当用户明确要求「多拉/再多一点/几百条」时再加大，上限 300。
    【能力边界】只有观看轨迹元数据，没有视频口播/完整剧情。
    若用户问「讲了什么/剧情/内容详解」而本结果不够：必须再调 web_search（不要用标题脑补后结束）。
    【省 token】不要顺手再调 followings / lookup_up；用户要细说或同意后再用。
    """
    if is_cancelled():
        return "Task cancelled; skip Bilibili fetch."
    if not bilibili_configured():
        return (
            "Bilibili not configured. Add to project root .env:\n"
            "  BILI_COOKIE=SESSDATA=...; bili_jct=...  (from browser while logged in)\n"
            "  BILI_UID=你的数字UID（可选）\n"
            "How: open bilibili.com → F12 → Network → any api request → Request Headers → Cookie\n"
            "Meanwhile you may call fetch_mock_bilibili_data for offline demo."
        )
    try:
        hn = max(5, min(int(history_n or 30), 300))
        full = fetch_bilibili_private_bundle(history_n=hn, uploads_n=0, include_favorites=False)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        full_text = json.dumps(full, ensure_ascii=False, separators=(",", ":"))
        with open(os.path.join(RAW_DIR, f"bilibili_private_{stamp}.json"), "w", encoding="utf-8") as f:
            f.write(full_text)
        with open(os.path.join(RAW_DIR, "bilibili_latest.json"), "w", encoding="utf-8") as f:
            f.write(full_text)
        slim = slim_bilibili_for_agent(full)
        return json.dumps(slim, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        return f"Bilibili fetch failed: {e}"


@tool
def fetch_bilibili_followings(max_n: int = 30) -> str:
    """
    拉取用户 B 站关注列表（mid/name/sign）。需 Cookie。
    【触发】仅当用户明确要看关注、或你在结尾轻问后用户同意「可以/展开」时调用。
    禁止在普通「最近看了什么」里顺手调用。
    """
    if is_cancelled():
        return "Task cancelled; skip Bilibili followings."
    if not bilibili_configured():
        return "Bilibili not configured. Set BILI_COOKIE in .env first."
    try:
        n = max(5, min(int(max_n or 30), 50))
        full = fetch_bilibili_followings_bundle(max_items=n)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        full_text = json.dumps(full, ensure_ascii=False, separators=(",", ":"))
        with open(os.path.join(RAW_DIR, f"bilibili_followings_{stamp}.json"), "w", encoding="utf-8") as f:
            f.write(full_text)
        with open(os.path.join(RAW_DIR, "bilibili_followings_latest.json"), "w", encoding="utf-8") as f:
            f.write(full_text)
        slim = slim_bilibili_for_agent(full)
        return json.dumps(slim, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        return f"Bilibili followings failed: {e}"


@tool
def lookup_bilibili_up(mid: int, uploads_n: int = 8) -> str:
    """
    按数字 mid 查看某 UP 的空间卡片 + 近期投稿标题/短简介（B 站 API，不是爬网页）。
    mid 可来自 history[].author_mid 或 followings[].mid。
    【触发】用户点名某个 UP / 要细看主页，或你提议后用户同意；禁止对历史里每个作者自动连环 lookup。
    【能力边界】只有 UP 资料与投稿元数据，不是视频正文/剧情。
    用户若继续追问「某个作品/系列讲了什么」，本工具不够 → 必须再 web_search，禁止仅凭标题完结。
    """
    if is_cancelled():
        return "Task cancelled; skip Bilibili UP lookup."
    if not bilibili_configured():
        return "Bilibili not configured. Set BILI_COOKIE in .env first."
    try:
        mid_i = int(mid)
        un = max(0, min(int(uploads_n or 8), 20))
        from apitools.bilibili.client import bilibili_cookie

        full = lookup_up_bundle(mid_i, bilibili_cookie(), uploads_n=un)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        full_text = json.dumps(full, ensure_ascii=False, separators=(",", ":"))
        with open(os.path.join(RAW_DIR, f"bilibili_up_{mid_i}_{stamp}.json"), "w", encoding="utf-8") as f:
            f.write(full_text)
        with open(os.path.join(RAW_DIR, "bilibili_up_latest.json"), "w", encoding="utf-8") as f:
            f.write(full_text)
        slim = slim_bilibili_for_agent(full)
        return json.dumps(slim, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        return f"Bilibili UP lookup failed: {e}"


@tool
def fetch_mock_bilibili_data() -> str:
    """离线 Mock B 站私人数据（_inbox/bilibili_private.json）。无 Cookie 时练工具路由用。"""
    if is_cancelled():
        return "Task cancelled; skip Bilibili fetch."
    return read_inbox_json("bilibili_private.json")
