"""沙箱工具：execute_python_code（固定通用能力，非数据源插件）。"""
from __future__ import annotations

import os
import subprocess
import sys

from langchain_core.tools import tool

from agent.cow import build_sandbox_cow_preamble
from agent.tools_common import (
    WORKSPACE_DIR,
    clear_active_proc,
    get_cow_session,
    is_cancelled,
    pkg_root,
    set_active_proc,
)


def _sandbox_import_preamble() -> str:
    """保证沙箱能 import ModernDashboard（cwd 在 workspace/）。"""
    return (
        "import sys\n"
        f"sys.path.insert(0, r'{pkg_root()}')\n"
        "from viz.dashboard import ModernDashboard, load_steam, load_spotify, load_bilibili, build_merged\n"
    )


@tool
def execute_python_code(code: str) -> str:
    """
    在本地沙箱执行 Python。工作目录为 workspace/。
    已预置: from viz.dashboard import ModernDashboard, load_steam, load_spotify, load_bilibili, build_merged
    出图请用 ModernDashboard(...).add_bar/add_pie/add_radar/add_bar_line(...).save('xxx.html')
    禁止手写整页 Plotly HTML / fig.write_html 当最终交付。
    """
    if is_cancelled():
        return "Task cancelled; skip code execution."

    script_path = os.path.join(WORKSPACE_DIR, "temp_sandbox_script.py")
    chunks = [_sandbox_import_preamble()]
    session = get_cow_session()
    if session is not None:
        chunks.append(
            build_sandbox_cow_preamble(
                session.workspace_dir,
                session.backup_root,
                session.initial_files,
            )
        )
    chunks.append(code)
    script_body = "\n".join(chunks)

    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script_body)

    proc = None
    try:
        proc = subprocess.Popen(
            [sys.executable, script_path],
            cwd=WORKSPACE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        set_active_proc(proc)

        try:
            stdout, stderr = proc.communicate(timeout=45)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            return "Timeout (>45s). Simplify plotting or check for infinite loops."

        if is_cancelled() or proc.returncode < 0:
            return "Task cancelled; code execution stopped."

        if proc.returncode == 0:
            return f"OK\nstdout:\n{stdout}"
        return (
            f"FAILED — fix code using stderr\n"
            f"stderr:\n{stderr}\n"
            f"stdout:\n{stdout}"
        )
    except Exception as e:
        return f"Sandbox internal error: {e}"
    finally:
        clear_active_proc(proc)
        if os.path.exists(script_path):
            os.remove(script_path)
