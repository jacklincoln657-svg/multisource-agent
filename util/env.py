"""进程内一次性加载仓库根目录 .env，避免各 apitools 反复扫盘。"""
from __future__ import annotations

from pathlib import Path

_loaded = False
# util/env.py → cyber_lobster_api_fusion → agent项目（.env 在仓库根）
_ROOT = Path(__file__).resolve().parents[2]


def load_project_env() -> None:
    global _loaded
    if _loaded:
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(_ROOT / ".env")
    except Exception:
        pass
    _loaded = True
