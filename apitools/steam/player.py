"""Steam Web API：个人库 / 游玩时长（需 API Key + SteamID）。

公开接口：
  https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/
  https://api.steampowered.com/IPlayerService/GetRecentlyPlayedGames/v1/
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

STEAM_API_BASE = "https://api.steampowered.com"


def steam_credentials() -> tuple[Optional[str], Optional[str]]:
    from util.env import load_project_env

    load_project_env()
    key = (os.getenv("STEAM_API_KEY") or "").strip()
    sid = (os.getenv("STEAM_ID") or "").strip()
    return (key or None, sid or None)


def steam_configured() -> bool:
    key, sid = steam_credentials()
    return bool(key and sid)


def _get_json(path: str, params: dict[str, Any], timeout: float = 20.0) -> dict[str, Any]:
    qs = urllib.parse.urlencode(params)
    url = f"{STEAM_API_BASE}{path}?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "cyber_lobster_api_fusion/0.3"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Steam HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Steam network error: {e.reason}") from e
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Steam response is not JSON: {e}") from e


def fetch_owned_games(key: str, steamid: str) -> dict[str, Any]:
    return _get_json(
        "/IPlayerService/GetOwnedGames/v1/",
        {
            "key": key,
            "steamid": steamid,
            "include_appinfo": 1,
            "include_played_free_games": 1,
            "format": "json",
        },
    )


def fetch_recent_games(key: str, steamid: str, count: int = 20) -> dict[str, Any]:
    return _get_json(
        "/IPlayerService/GetRecentlyPlayedGames/v1/",
        {
            "key": key,
            "steamid": steamid,
            "count": count,
            "format": "json",
        },
    )


def normalize_owned_payload(raw: dict[str, Any], top_n: int = 30) -> dict[str, Any]:
    """压成与 Mock 相近结构，按总时长降序截断 top_n。"""
    resp = raw.get("response") or {}
    games = list(resp.get("games") or [])
    cleaned = []
    for g in games:
        name = g.get("name") or f"app_{g.get('appid')}"
        cleaned.append(
            {
                "appid": g.get("appid"),
                "name": name,
                "playtime_forever": int(g.get("playtime_forever") or 0),
                "playtime_2weeks": int(g.get("playtime_2weeks") or 0),
            }
        )
    cleaned.sort(key=lambda x: x["playtime_forever"], reverse=True)
    if top_n > 0:
        cleaned = cleaned[:top_n]
    return {
        "source": "steam_live",
        "endpoint": "GetOwnedGames",
        "response": {
            "game_count": len(cleaned),
            "games_total_owned": int(resp.get("game_count") or len(games)),
            "games": cleaned,
        },
    }


def fetch_steam_playtime_bundle(top_n: int = 30) -> dict[str, Any]:
    """拉取拥有游戏（含名称）并规范化；附带 recent 原文摘要。"""
    key, sid = steam_credentials()
    if not key or not sid:
        raise RuntimeError(
            "Steam not configured. Set STEAM_API_KEY and STEAM_ID in project root .env"
        )
    owned = fetch_owned_games(key, sid)
    normalized = normalize_owned_payload(owned, top_n=top_n)
    try:
        recent = fetch_recent_games(key, sid, count=min(20, top_n))
        normalized["recent"] = recent.get("response") or {}
    except Exception as e:
        normalized["recent_error"] = str(e)
    return normalized
