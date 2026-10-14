"""B 站数据源插件。"""
from __future__ import annotations

from pathlib import Path

from plugins.bilibili.tools import (
    fetch_bilibili_data,
    fetch_bilibili_followings,
    fetch_mock_bilibili_data,
    lookup_bilibili_up,
)
from apitools.bilibili import bilibili_configured

PLUGIN_ID = "bilibili"
TITLE = "B 站"
TOOLS = [
    fetch_bilibili_data,
    fetch_bilibili_followings,
    lookup_bilibili_up,
    fetch_mock_bilibili_data,
]
SKILL_FILE = Path(__file__).with_name("SKILL.md")


def status_line() -> str:
    if bilibili_configured():
        return "已配置 BILI_COOKIE（私人观看历史可用 fetch_bilibili_data）"
    return (
        "未配置 B 站：请在根目录 .env 填写 BILI_COOKIE（含 SESSDATA）；"
        "此前可用 fetch_mock_bilibili_data"
    )
