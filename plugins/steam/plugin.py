"""Steam 数据源插件：声明 Tool + SKILL。"""
from __future__ import annotations

from pathlib import Path

from plugins.steam.tools import (
    fetch_mock_steam_data,
    fetch_steam_data,
    lookup_steam_app,
    search_steam_store,
)
from apitools.steam import steam_configured

PLUGIN_ID = "steam"
TITLE = "Steam"
TOOLS = [
    fetch_steam_data,
    fetch_mock_steam_data,
    search_steam_store,
    lookup_steam_app,
]
SKILL_FILE = Path(__file__).with_name("SKILL.md")


def status_line() -> str:
    if steam_configured():
        return "已配置 STEAM_API_KEY + STEAM_ID，优先用 fetch_steam_data"
    return (
        "未配置 Steam：请用户在根目录 .env 填写 STEAM_API_KEY 与 STEAM_ID；"
        "此前可用 fetch_mock_steam_data"
    )
