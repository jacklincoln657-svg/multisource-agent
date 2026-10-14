"""网页证据插件（Tavily + web_fetch）。"""
from __future__ import annotations

from pathlib import Path

from plugins.web.tools import web_fetch, web_search
from apitools.web import web_search_configured

PLUGIN_ID = "web"
TITLE = "网页证据"
TOOLS = [web_search, web_fetch]
SKILL_FILE = Path(__file__).with_name("SKILL.md")


def status_line() -> str:
    if web_search_configured():
        return "已配置网页搜索（Tavily）"
    return "未配置网页搜索：请在根目录 .env 填写 TAVILY_API_KEY（https://app.tavily.com/home）"
