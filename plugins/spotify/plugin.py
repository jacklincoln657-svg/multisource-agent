"""Spotify（当前仅 Mock）插件。"""
from __future__ import annotations

from pathlib import Path

from plugins.spotify.tools import fetch_mock_spotify_data

PLUGIN_ID = "spotify"
TITLE = "Spotify"
TOOLS = [fetch_mock_spotify_data]
SKILL_FILE = Path(__file__).with_name("SKILL.md")


def status_line() -> str:
    return "一期仅 Mock（workspace/_inbox）；真 OAuth 未接，可用 fetch_mock_spotify_data 演示融合"
