"""Spotify 插件 @tool（一期仅 Mock）。"""
from __future__ import annotations

from langchain_core.tools import tool

from agent.tools_common import is_cancelled, read_inbox_json


@tool
def fetch_mock_spotify_data() -> str:
    """获取用户 Spotify 侧 Mock 数据（含 top_tracks 听歌列表 + game_affinity 与 Steam 对齐的评分）。"""
    if is_cancelled():
        return "Task cancelled; skip Spotify fetch."
    return read_inbox_json("spotify_stats.json")
