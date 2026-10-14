"""轻量冒烟：启动时插件 Registry（不调 LLM）。

运行（在 cyber_lobster_api_fusion 目录）:
  python -m scripts.smoke_plugins
或:
  python scripts/smoke_plugins.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugins._registry import load_plugins


def main() -> None:
    reg = load_plugins(force_reload=True)
    ids = [p.plugin_id for p in reg.plugins]
    assert ids == ["steam", "bilibili", "spotify", "feishu", "web"], ids
    tools = reg.tools
    assert len(tools) >= 20, len(tools)
    block = reg.render_skills_block()
    for needle in ("本站优先", "quote_hints", "HARD_STOP", "fetch_steam_data", "info_cards"):
        assert needle in block, f"missing in skills: {needle}"
    # 二次 load 应命中缓存（同一对象）
    reg2 = load_plugins(force_reload=False)
    assert reg2 is reg
    print("[OK] plugins:", ids)
    print("[OK] tool count:", len(tools))
    print("[OK] skill block chars:", len(block))
    print("=== smoke_plugins PASSED ===")


if __name__ == "__main__":
    main()
