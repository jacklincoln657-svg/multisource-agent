"""工作区路径、COW、取消、Tool 预算硬闸、inbox/raw 读写（各插件共用）。"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from typing import Any, Callable, Optional

from agent.cow import CowSession

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE_DIR = os.path.join(_PKG_ROOT, "workspace")
INBOX_DIR = os.path.join(WORKSPACE_DIR, "_inbox")
RAW_DIR = os.path.join(WORKSPACE_DIR, "raw")
os.makedirs(INBOX_DIR, exist_ok=True)
os.makedirs(RAW_DIR, exist_ok=True)

_cancel_checker: Optional[Callable[[], bool]] = None
_active_proc = None
_proc_lock = threading.Lock()
_cow_session: Optional[CowSession] = None
_cow_lock = threading.Lock()

WEB_SEARCH_LIMIT = 2
WEB_FETCH_LIMIT = 2
SINGLE_EVENT_LIMIT = 1
_budget_lock = threading.Lock()
_tool_budget: dict[str, int] = {
    "web_search": 0,
    "web_fetch": 0,
    "create_feishu_event": 0,
}


def reset_tool_budgets() -> None:
    """每轮对话开始时清零；由 app / CLI 调用。"""
    with _budget_lock:
        _tool_budget["web_search"] = 0
        _tool_budget["web_fetch"] = 0
        _tool_budget["create_feishu_event"] = 0


def consume_budget(name: str, limit: int, refuse_msg: str) -> Optional[str]:
    """超限返回拒绝文案（给 Agent），否则递增并返回 None。"""
    with _budget_lock:
        used = int(_tool_budget.get(name) or 0)
        if used >= limit:
            return refuse_msg
        _tool_budget[name] = used + 1
        return None


def set_cancel_checker(fn: Optional[Callable[[], bool]]) -> None:
    global _cancel_checker
    _cancel_checker = fn


def begin_cow_session() -> CowSession:
    global _cow_session
    with _cow_lock:
        if _cow_session is not None:
            _cow_session.cleanup()
        _cow_session = CowSession(WORKSPACE_DIR)
        return _cow_session


def get_cow_session() -> Optional[CowSession]:
    with _cow_lock:
        return _cow_session


def end_cow_session(rollback: bool = False) -> tuple[int, int]:
    global _cow_session
    with _cow_lock:
        session = _cow_session
        _cow_session = None
    if session is None:
        return 0, 0
    restored = deleted = 0
    try:
        if rollback:
            restored, deleted = session.rollback()
    finally:
        session.cleanup()
    return restored, deleted


def is_cancelled() -> bool:
    return bool(_cancel_checker and _cancel_checker())


def request_kill_sandbox() -> None:
    with _proc_lock:
        proc = _active_proc
    if proc is not None and proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass


def set_active_proc(proc: Any) -> None:
    global _active_proc
    with _proc_lock:
        _active_proc = proc


def clear_active_proc(proc: Any) -> None:
    global _active_proc
    with _proc_lock:
        if _active_proc is proc:
            _active_proc = None


def read_inbox_json(filename: str) -> str:
    path = os.path.join(INBOX_DIR, filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        json.loads(raw)
        return raw
    except FileNotFoundError:
        return f"数据文件不存在: {path}"
    except json.JSONDecodeError as e:
        return f"JSON 解析失败 ({filename}): {e}"
    except Exception as e:
        return f"读取失败 ({filename}): {e}"


def dump_raw(prefix: str, payload: Any) -> str:
    """全量落盘 raw/（紧凑 JSON）；返回同文本。"""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    with open(os.path.join(RAW_DIR, f"{prefix}_{stamp}.json"), "w", encoding="utf-8") as f:
        f.write(text)
    with open(os.path.join(RAW_DIR, f"{prefix}_latest.json"), "w", encoding="utf-8") as f:
        f.write(text)
    return text


def pkg_root() -> str:
    return _PKG_ROOT
