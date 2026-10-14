"""Steam 插件 @tool。"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

from langchain_core.tools import tool

from agent.tools_common import RAW_DIR, is_cancelled, read_inbox_json
from apitools.steam import (
    fetch_steam_playtime_bundle,
    lookup_app,
    search_and_enrich,
    slim_results_for_agent,
    steam_configured,
)

_owned_appids_cache: dict[str, Any] = {"mtime": None, "ids": set()}
_steam_card_mem_cache: dict[str, Any] = {"mtime": None, "hint": "", "cards": []}


def _owned_appids_from_disk() -> set[int]:
    latest = os.path.join(RAW_DIR, "steam_latest.json")
    try:
        mtime = os.path.getmtime(latest)
    except OSError:
        return set()
    if _owned_appids_cache["mtime"] == mtime:
        return set(_owned_appids_cache["ids"])
    try:
        with open(latest, "r", encoding="utf-8") as f:
            payload = json.load(f)
        games = ((payload.get("response") or {}).get("games")) or []
        out: set[int] = set()
        for g in games:
            aid = g.get("appid")
            if aid is not None:
                out.add(int(aid))
        _owned_appids_cache["mtime"] = mtime
        _owned_appids_cache["ids"] = out
        return set(out)
    except Exception:
        return set()


def _steam_card_memory_path() -> str:
    return os.path.join(RAW_DIR, "steam_card_memory.json")


def _remember_store_cards(cards: list) -> None:
    """会话级短记忆：名称↔appid，供下一轮 lookup（因 tool 轨迹会被裁掉）。"""
    path = _steam_card_memory_path()
    prev = list(_steam_card_mem_cache.get("cards") or [])
    if not prev:
        try:
            with open(path, "r", encoding="utf-8") as f:
                prev = list((json.load(f) or {}).get("cards") or [])
        except Exception:
            prev = []
    by_id = {}
    for c in prev + list(cards or []):
        aid = c.get("appid")
        name = (c.get("name") or "").strip()
        if aid is None or not name:
            continue
        by_id[int(aid)] = {"appid": int(aid), "name": name}
    merged = list(by_id.values())[-20:]
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {"updated": datetime.now().isoformat(timespec="seconds"), "cards": merged},
                f,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        _steam_card_mem_cache["mtime"] = os.path.getmtime(path)
        _steam_card_mem_cache["cards"] = merged
        lines = [f"- {c.get('name')} → appid {c.get('appid')}" for c in merged[-12:]]
        _steam_card_mem_cache["hint"] = (
            (
                "[商店卡片备忘 — 仅供 lookup_steam_app；勿对用户播报 appid]\n"
                + "\n".join(lines)
            )
            if lines
            else ""
        )
    except Exception:
        pass


def load_steam_card_memory_hint() -> str:
    """给下一轮模型的短备忘（无 HTTP；mtime 命中则不读盘）。"""
    path = _steam_card_memory_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    if _steam_card_mem_cache["mtime"] == mtime and _steam_card_mem_cache.get("hint") is not None:
        return str(_steam_card_mem_cache["hint"] or "")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cards = list((data or {}).get("cards") or [])
    except Exception:
        return ""
    _steam_card_mem_cache["mtime"] = mtime
    _steam_card_mem_cache["cards"] = cards
    if not cards:
        _steam_card_mem_cache["hint"] = ""
        return ""
    lines = [f"- {c.get('name')} → appid {c.get('appid')}" for c in cards[-12:]]
    hint = (
        "[商店卡片备忘 — 仅供 lookup_steam_app；勿对用户播报 appid]\n"
        + "\n".join(lines)
    )
    _steam_card_mem_cache["hint"] = hint
    return hint


@tool
def fetch_steam_data(top_n: int = 30) -> str:
    """
    拉取真实 Steam 游玩数据（GetOwnedGames，按总时长取前 top_n）。
    需要根目录 .env 中 STEAM_API_KEY 与 STEAM_ID。
    返回 JSON：response.games[].name / playtime_forever(分钟) / appid；并落盘 workspace/raw/。
    """
    if is_cancelled():
        return "Task cancelled; skip Steam fetch."
    if not steam_configured():
        return (
            "Steam not configured. Add to project root .env:\n"
            "  STEAM_API_KEY=...\n"
            "  STEAM_ID=64-bit steam id\n"
            "Key: https://steamcommunity.com/dev/apikey\n"
            "Meanwhile you may call fetch_mock_steam_data for offline demo."
        )
    try:
        n = int(top_n) if top_n else 30
        n = max(5, min(n, 100))
        payload = fetch_steam_playtime_bundle(top_n=n)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_name = f"steam_owned_{stamp}.json"
        out_path = os.path.join(RAW_DIR, out_name)
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
        latest = os.path.join(RAW_DIR, "steam_latest.json")
        with open(latest, "w", encoding="utf-8") as f:
            f.write(text)
        return text
    except Exception as e:
        return f"Steam fetch failed: {e}"


@tool
def fetch_mock_steam_data() -> str:
    """离线 Mock Steam 数据（_inbox/steam_playtime.json）。无真 Key 或只想练融合时用。"""
    if is_cancelled():
        return "Task cancelled; skip Steam fetch."
    return read_inbox_json("steam_playtime.json")


@tool
def search_steam_store(query: str, max_results: int = 4, exclude_owned: bool = True) -> str:
    """
    在 Steam 商店按关键词搜索，返回标准「商店卡片」列表（找候选 / 推荐时用）。
    构造英文或中文 query（如 "deck builder"）；优先一次准词，最多再换词一次。
    exclude_owned=True 时排除 workspace/raw/steam_latest.json 中已有 appid。
    每张卡片固定字段：appid, name, genres, short_description, store_url, price,
    review_label, positive_pct, total_reviews, metacritic。
    用户追问已出现过的某一款时，请改用 lookup_steam_app(appid)，不要再模糊搜索。
    """
    if is_cancelled():
        return "Task cancelled; skip Steam store search."
    q = (query or "").strip()
    if not q:
        return "query is required (e.g. 'deck builder' or 'tactical shooter')."
    try:
        n = int(max_results) if max_results else 4
        n = max(3, min(n, 6))
        exclude = _owned_appids_from_disk() if exclude_owned else set()
        payload = search_and_enrich(
            q,
            max_results=n,
            exclude_appids=exclude,
            with_details=True,
            with_reviews=True,
        )
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        full_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        out_path = os.path.join(RAW_DIR, f"steam_store_{stamp}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(full_text)
        latest = os.path.join(RAW_DIR, "steam_store_latest.json")
        with open(latest, "w", encoding="utf-8") as f:
            f.write(full_text)
        slim = slim_results_for_agent(payload)
        _remember_store_cards(slim.get("cards") or [])
        return json.dumps(slim, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        return f"Steam store search failed: {e}"


@tool
def lookup_steam_app(appid: int) -> str:
    """
    按 Steam appid 拉取单张标准商店卡片（详情+口碑，比关键词搜索快很多）。
    用于：上一轮推荐里已出现的游戏、用户追问「详细说说 XX」、已知 appid。
    卡片字段同 search_steam_store：appid/name/genres/short_description/store_url/price/
    review_label/positive_pct/total_reviews/metacritic。
    """
    if is_cancelled():
        return "Task cancelled; skip Steam app lookup."
    try:
        aid = int(appid)
    except (TypeError, ValueError):
        return "appid must be an integer (Steam application id)."
    try:
        payload = lookup_app(aid)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        full_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        out_path = os.path.join(RAW_DIR, f"steam_lookup_{aid}_{stamp}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(full_text)
        latest = os.path.join(RAW_DIR, "steam_lookup_latest.json")
        with open(latest, "w", encoding="utf-8") as f:
            f.write(full_text)
        slim = slim_results_for_agent(
            {
                "source": "steam_store_lookup",
                "appid": aid,
                "cards": [payload["card"]],
            }
        )
        _remember_store_cards(slim.get("cards") or [])
        return json.dumps(slim, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        return f"Steam app lookup failed: {e}"
