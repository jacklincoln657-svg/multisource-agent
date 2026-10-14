"""启动时扫描 plugins/*/plugin.py，缓存 Tool 与 SKILL（非运行时热插拔）。"""
from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

_PLUGINS_ROOT = Path(__file__).resolve().parent

# 稳定顺序（展示与 Prompt 拼接）
_PLUGIN_ORDER = ("steam", "bilibili", "spotify", "feishu", "web")


@dataclass
class LoadedPlugin:
    plugin_id: str
    title: str
    tools: list[Any]
    skill_text: str
    status_line: str = ""


@dataclass
class PluginRegistry:
    plugins: list[LoadedPlugin] = field(default_factory=list)

    @property
    def tools(self) -> list[Any]:
        out: list[Any] = []
        seen: set[int] = set()
        for p in self.plugins:
            for t in p.tools:
                i = id(t)
                if i in seen:
                    continue
                seen.add(i)
                out.append(t)
        return out

    def render_skills_block(self) -> str:
        chunks: list[str] = []
        for p in self.plugins:
            head = f"### {p.title}（plugin=`{p.plugin_id}`）"
            if p.status_line:
                head += f"\n状态：{p.status_line}"
            body = (p.skill_text or "").strip()
            chunks.append(head + ("\n" + body if body else ""))
        return "\n\n".join(chunks)


_REGISTRY: PluginRegistry | None = None


def _load_one(name: str) -> LoadedPlugin | None:
    mod_path = f"plugins.{name}.plugin"
    try:
        mod = importlib.import_module(mod_path)
    except Exception as e:
        logger.warning("skip plugin %s: import failed: %s", name, e)
        return None

    plugin_id = str(getattr(mod, "PLUGIN_ID", name) or name)
    title = str(getattr(mod, "TITLE", plugin_id) or plugin_id)
    tools = list(getattr(mod, "TOOLS", None) or [])
    skill_file = getattr(mod, "SKILL_FILE", None)
    if skill_file is None:
        skill_path = _PLUGINS_ROOT / name / "SKILL.md"
    else:
        skill_path = Path(skill_file)

    skill_text = ""
    if skill_path.is_file():
        skill_text = skill_path.read_text(encoding="utf-8")

    status_fn: Callable[[], str] | None = getattr(mod, "status_line", None)
    status = ""
    if callable(status_fn):
        try:
            status = str(status_fn() or "")
        except Exception as e:
            status = f"（状态探测失败: {e}）"

    return LoadedPlugin(
        plugin_id=plugin_id,
        title=title,
        tools=tools,
        skill_text=skill_text,
        status_line=status,
    )


def _discover_names() -> list[str]:
    found = {
        p.name
        for p in _PLUGINS_ROOT.iterdir()
        if p.is_dir() and (p / "plugin.py").is_file() and not p.name.startswith("_")
    }
    ordered = [n for n in _PLUGIN_ORDER if n in found]
    rest = sorted(found - set(ordered))
    return ordered + rest


def load_plugins(*, force_reload: bool = False) -> PluginRegistry:
    """默认只加载一次并缓存；force_reload 仅供显式调试，产品口径仍是「改插件需重启」。"""
    global _REGISTRY
    if _REGISTRY is not None and not force_reload:
        return _REGISTRY

    plugins: list[LoadedPlugin] = []
    for name in _discover_names():
        loaded = _load_one(name)
        if loaded is not None:
            plugins.append(loaded)

    _REGISTRY = PluginRegistry(plugins=plugins)
    return _REGISTRY


def get_registry() -> PluginRegistry:
    return load_plugins(force_reload=False)
