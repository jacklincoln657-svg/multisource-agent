"""
个人画像 Agent UI（DeepSeek / Gemini 风格）：
左侧会话历史 · 右侧主聊天。产物以聊天气泡内链接交付（/open/...）。
"""
from __future__ import annotations

import os
import re
import threading
from urllib.parse import quote

import dash
import dash_bootstrap_components as dbc
from dash import ALL, Input, Output, State, ctx, dcc, html, no_update
from flask import Response, abort, jsonify, send_from_directory
from langchain_core.callbacks import BaseCallbackHandler

from agent.core import (
    AVAILABLE_MODELS,
    clear_agent_cache,
    get_agent,
    get_llm,
    llm,
    model_label,
    normalize_model_id,
)
from context.dialogue import (
    archive_session,
    build_model_context,
    clear_active_session,
    delete_archived_session,
    list_archived_sessions,
    load_active_session,
    load_archived_session,
    save_active_session,
    strip_clear_keywords,
    wants_new_session,
)
from agent.tools_common import (
    WORKSPACE_DIR,
    begin_cow_session,
    end_cow_session,
    request_kill_sandbox,
    reset_tool_budgets,
    set_cancel_checker,
)
from context.chat_cache import (
    clear_session_cache,
    load_chat_cache_index_hint,
    set_active_session_key,
)
from plugins.steam.tools import load_steam_card_memory_hint

# 进程启动 / 热重载 app 时清掉 Agent 单例，确保 system_prompt / tools 变更生效
clear_agent_cache()

app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.FLATLY],
    suppress_callback_exceptions=True,
)

# 主界面默认空白（Gemini 式）：刷新/打开页面 = 新会话。
# 进行中草稿仍会落到 _active.json（防进程崩溃丢消息）；刷新时自动归档到「历史」。
chat_history: list = []
conversation_summary = ""
conversation_summary_covers = 0  # 摘要已覆盖的用户轮次数（弹性窗口水位线）

_job_lock = threading.Lock()
_job = {
    "running": False,
    "error": None,
    "cancel": threading.Event(),
    "model_id": None,
    "thinking": False,
}

# 点历史只切换显示，不写 _active、不 archive。
# session_epoch：新对话后作废进行中的后台任务写盘。
_nav = {
    "loaded_from": None,
    "session_epoch": 0,
}


def _archive_and_reset_to_blank(*, cancel_job: bool = True) -> str | None:
    """把当前草稿归档到历史并清空主界面。返回归档相对路径（若有）。"""
    global chat_history, conversation_summary, conversation_summary_covers
    with _job_lock:
        if cancel_job and _job["running"]:
            cancel = _job.get("cancel")
            if cancel:
                cancel.set()
            request_kill_sandbox()
        viewing = _nav.get("loaded_from")
        if viewing:
            # 正在看历史：归档磁盘上的真实草稿，不要把历史再存一份
            draft_h, draft_s, draft_c = load_active_session(WORKSPACE_DIR)
            rel = (
                archive_session(
                    draft_h, draft_s, WORKSPACE_DIR, llm=llm, summary_covers=draft_c
                )
                if draft_h
                else None
            )
        elif chat_history:
            rel = archive_session(
                chat_history,
                conversation_summary,
                WORKSPACE_DIR,
                llm=llm,
                summary_covers=conversation_summary_covers,
            )
        else:
            draft_h, draft_s, draft_c = load_active_session(WORKSPACE_DIR)
            rel = (
                archive_session(
                    draft_h, draft_s, WORKSPACE_DIR, llm=llm, summary_covers=draft_c
                )
                if draft_h
                else None
            )
        chat_history = []
        conversation_summary = ""
        conversation_summary_covers = 0
        _nav["loaded_from"] = None
        _nav["session_epoch"] = int(_nav.get("session_epoch") or 0) + 1
        clear_session_cache(f"epoch-{int(_nav['session_epoch']) - 1}")
        clear_active_session(WORKSPACE_DIR)
        save_active_session([], "", WORKSPACE_DIR, summary_covers=0)
        _job["running"] = False
    return rel

_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
_TABLE_EXT = {".csv", ".xlsx", ".xls"}
_TEXT_EXT = {".txt", ".md", ".json", ".py", ".log", ".tsv"}
_FILE_IN_TEXT = re.compile(
    r"(?<![\w/])((?:[\w.-]+/)*[\w.-]+\.(?:html|csv|xlsx|xls|json|md|png|jpg|jpeg))",
    re.IGNORECASE,
)


def rel_workspace(path: str) -> str:
    return os.path.relpath(path, WORKSPACE_DIR).replace("\\", "/")


def resolve_workspace_path(rel_path: str) -> str:
    rel = (rel_path or "").replace("\\", "/").lstrip("/")
    full = (
        os.path.normpath(os.path.join(WORKSPACE_DIR, *rel.split("/")))
        if rel
        else os.path.normpath(WORKSPACE_DIR)
    )
    root = os.path.normpath(WORKSPACE_DIR)
    if full != root and not full.startswith(root + os.sep):
        abort(404)
    return full


def build_standalone_table_page(title: str, df: pd.DataFrame) -> str:
    from html import escape

    safe_title = escape(title)
    table = df.astype(object).where(pd.notnull(df), "").to_html(index=False, border=0)
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/><title>{safe_title}</title>
<style>
body{{font-family:system-ui,sans-serif;margin:24px;background:#f8f9fa}}
table{{border-collapse:collapse;width:100%;background:#fff}}
th,td{{border:1px solid #dee2e6;padding:8px;text-align:center}}
th{{background:#e9ecef}}
</style></head>
<body><h1>{safe_title}</h1>{table}</body></html>"""


def _persist_active_unlocked():
    save_active_session(
        chat_history,
        conversation_summary,
        WORKSPACE_DIR,
        summary_covers=conversation_summary_covers,
    )


def _extract_file_links(text: str) -> list[str]:
    found = []
    seen = set()
    for m in _FILE_IN_TEXT.finditer(text or ""):
        rel = m.group(1).replace("\\", "/").lstrip("./")
        if rel.startswith("workspace/"):
            rel = rel[len("workspace/") :]
        if rel in seen:
            continue
        path = os.path.join(WORKSPACE_DIR, *rel.split("/"))
        if os.path.isfile(path):
            seen.add(rel)
            found.append(rel)
    return found


def build_chat_ui(history, trailing=None):
    """用户/助手气泡；助手消息下挂可点击的产物链接。"""
    chat_ui = []
    for msg in history:
        msg_type = msg.get("role") if isinstance(msg, dict) else getattr(msg, "type", "")
        msg_content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", "")
        if not isinstance(msg_content, str):
            msg_content = str(msg_content or "")

        if msg_type in ("user", "human"):
            chat_ui.append(
                html.Div(
                    [
                        html.Div("你", className="chat-role user-role"),
                        html.Div(msg_content, className="chat-bubble user-bubble"),
                    ],
                    className="chat-row user-row",
                )
            )
            continue

        if msg_type not in ("assistant", "ai") or not msg_content.strip():
            continue
        tool_calls = (
            msg.get("tool_calls") if isinstance(msg, dict) else getattr(msg, "tool_calls", None)
        )
        if tool_calls:
            continue

        links = _extract_file_links(msg_content)
        link_row = []
        for rel in links:
            link_row.append(
                html.A(
                    f"📎 {rel}",
                    href=f"/open/{quote(rel)}",
                    target="_blank",
                    rel="noopener noreferrer",
                    className="artifact-chip",
                )
            )

        chat_ui.append(
            html.Div(
                [
                    html.Div("仲町阿拉蕾", className="chat-role ai-role"),
                    dcc.Markdown(
                        msg_content,
                        className="chat-bubble ai-bubble",
                        dangerously_allow_html=False,
                    ),
                    html.Div(link_row, className="artifact-row") if link_row else None,
                ],
                className="chat-row ai-row",
            )
        )

    if trailing:
        chat_ui.append(trailing)
    if not chat_ui:
        chat_ui.append(
            html.Div(
                [
                    html.H4("仲町阿拉蕾的agent小屋", className="mb-2"),
                    html.P(
                        "仲町的橘猫画像助手～试试：拉取 Steam 游玩数据并画一张看板。",
                        className="text-muted mb-0",
                    ),
                ],
                className="empty-hint",
            )
        )
    return chat_ui


def build_history_sidebar(active_label: str = "当前对话"):
    loaded = _nav.get("loaded_from")
    items = [
        dbc.Button(
            "+ 新对话",
            id="btn-new-session",
            color="primary",
            className="w-100 mb-3",
            n_clicks=0,
        ),
        html.Div(active_label, className="sidebar-section-title"),
        html.Div(
            dbc.Button(
                "● 进行中" if not loaded else "○ 进行中草稿",
                id="btn-active-session",
                color="light",
                className=(
                    "w-100 text-start mb-2 sidebar-item"
                    + (" active-item" if not loaded else "")
                ),
                n_clicks=0,
            )
        ),
        html.Div("历史会话", className="sidebar-section-title mt-3"),
    ]
    archived = list_archived_sessions(WORKSPACE_DIR)
    if not archived:
        items.append(html.Div("暂无归档", className="text-muted small px-1"))
    for s in archived:
        label = s["topic"] or "未命名"
        sub = s["archived_at"] or ""
        is_on = loaded == s["filename"]
        items.append(
            html.Div(
                [
                    dbc.Button(
                        [
                            html.Div(label, className="text-truncate hist-title"),
                            html.Small(sub, className="text-muted"),
                        ],
                        id={"type": "hist-session", "file": s["filename"]},
                        color="light",
                        className=(
                            "hist-open-btn text-start"
                            + (" active-item" if is_on else "")
                        ),
                        n_clicks=0,
                    ),
                    html.Button(
                        "🗑",
                        id={"type": "hist-del", "file": s["filename"]},
                        className="hist-del-btn",
                        title="删除此会话",
                        n_clicks=0,
                        type="button",
                    ),
                ],
                className="hist-row" + (" active-item" if is_on else ""),
            )
        )
    return html.Div(items, id="history-list")


class _CancelCallback(BaseCallbackHandler):
    def __init__(self, cancel_event: threading.Event):
        super().__init__()
        self._cancel = cancel_event

    def _raise_if_cancelled(self):
        if self._cancel.is_set():
            raise InterruptedError("用户中断")

    def on_llm_start(self, *args, **kwargs):
        self._raise_if_cancelled()

    def on_tool_start(self, *args, **kwargs):
        self._raise_if_cancelled()


def _run_agent_job(full_history_snapshot, model_id=None, epoch=None, thinking=False):
    global chat_history, conversation_summary, conversation_summary_covers
    cancel_event = threading.Event()
    begin_cow_session()
    reset_tool_budgets()
    mid = normalize_model_id(model_id)
    think = bool(thinking)
    active_llm = get_llm(mid, thinking=think)
    active_agent = get_agent(mid, thinking=think)

    with _job_lock:
        _job["cancel"] = cancel_event
        summary_snap = conversation_summary
        covers_snap = conversation_summary_covers
        start_epoch = _nav["session_epoch"] if epoch is None else epoch

    # 方案一：按会话 epoch 隔离聊天缓存
    set_active_session_key(f"epoch-{start_epoch}")
    set_cancel_checker(lambda: cancel_event.is_set())
    finished_ok = False

    def _stale() -> bool:
        return start_epoch != _nav.get("session_epoch")

    try:
        model_msgs, new_summary, new_covers = build_model_context(
            full_history_snapshot,
            summary_snap,
            active_llm,
            summary_covers=covers_snap,
        )
        hint = load_steam_card_memory_hint()
        chat_hint = load_chat_cache_index_hint()  # 仅索引钥匙，不含原文
        inject: list = []
        if hint:
            inject.extend(
                [
                    {"role": "user", "content": hint},
                    {
                        "role": "assistant",
                        "content": "已记下近期商店卡片的 appid，追问时优先 lookup_steam_app。",
                    },
                ]
            )
        if chat_hint:
            inject.extend(
                [
                    {"role": "user", "content": chat_hint},
                    {
                        "role": "assistant",
                        "content": (
                            "已知本会话有白名单聊天缓存。"
                            "追问原话优先用 info_cards.quote_hints 调 query_cached_chat；"
                            "用户改 hours/recent_n 或要求重拉时再 fetch_feishu_chat_digest，"
                            "勿把上一轮结论当成当前群聊真相。"
                        ),
                    },
                ]
            )
        if inject:
            insert_at = 0
            if (
                model_msgs
                and isinstance(model_msgs[0], dict)
                and str(model_msgs[0].get("content") or "").startswith("[会话摘要")
            ):
                insert_at = 2 if len(model_msgs) >= 2 else 1
            model_msgs = list(model_msgs[:insert_at]) + inject + list(model_msgs[insert_at:])
        if cancel_event.is_set():
            raise InterruptedError("用户中断")

        final_messages = None
        for state in active_agent.stream(
            {"messages": model_msgs},
            config={"callbacks": [_CancelCallback(cancel_event)]},
            stream_mode="values",
        ):
            if cancel_event.is_set():
                raise InterruptedError("用户中断")
            if _stale():
                raise InterruptedError("会话已切换，丢弃本轮结果")
            final_messages = state.get("messages", final_messages)

        if final_messages is None:
            raise RuntimeError("Agent 未返回任何消息")

        newly = final_messages[len(model_msgs) :]
        with _job_lock:
            if _stale():
                _job["error"] = "stale"
                _job["running"] = False
            else:
                chat_history = list(full_history_snapshot) + list(newly)
                conversation_summary = new_summary
                conversation_summary_covers = new_covers
                _nav["loaded_from"] = None
                _persist_active_unlocked()
                _job["error"] = None
                _job["running"] = False
                finished_ok = True
    except InterruptedError:
        request_kill_sandbox()
        restored, deleted_new = end_cow_session(rollback=True)
        tip = (
            f"已中断。写时备份回退：恢复 {restored} 个文件，删除本轮新建 {deleted_new} 个。"
        )
        with _job_lock:
            if _stale():
                _job["error"] = "stale"
                _job["running"] = False
            else:
                chat_history = list(full_history_snapshot) + [{"role": "ai", "content": tip}]
                _persist_active_unlocked()
                _job["error"] = "cancelled"
                _job["running"] = False
    except Exception as e:
        with _job_lock:
            if _stale():
                _job["error"] = "stale"
                _job["running"] = False
            else:
                if chat_history and (
                    (isinstance(chat_history[-1], dict) and chat_history[-1].get("role") == "user")
                    or getattr(chat_history[-1], "type", None) == "human"
                ):
                    chat_history = chat_history[:-1]
                chat_history = list(chat_history) + [{"role": "ai", "content": f"系统错误: {e}"}]
                _persist_active_unlocked()
                _job["error"] = str(e)
                _job["running"] = False
    finally:
        set_cancel_checker(None)
        if finished_ok:
            end_cow_session(rollback=False)
        elif _job.get("error") not in ("cancelled", "stale"):
            end_cow_session(rollback=False)


@app.server.route("/health")
def health():
    """运维探针：各源配置 + 飞书 OAuth/scope + 当前会话聊天缓存索引（不写任何数据）。"""
    from apitools.steam import steam_configured
    from apitools.bilibili import bilibili_configured
    from apitools.web import web_search_configured
    from apitools.feishu import feishu_auth_health
    from context.chat_cache import cache_index

    feishu = feishu_auth_health()
    ok = bool(feishu.get("ok"))
    body = {
        "ok": ok,
        "steam": steam_configured(),
        "bilibili": bilibili_configured(),
        "web_search": web_search_configured(),
        "feishu": feishu,
        "chat_session_cache": cache_index(),
    }
    return jsonify(body), (200 if ok or not feishu.get("app_configured") else 503)


@app.server.route("/open/<path:filename>")
def open_workspace_file(filename):
    """聊天产物链接：html/图直接打开；表与文本做简单页。"""
    path = resolve_workspace_path(filename)
    if not os.path.isfile(path):
        abort(404)
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    directory = os.path.dirname(path)

    if ext in _IMAGE_EXT or ext == ".html":
        return send_from_directory(directory, name)

    if ext in _TABLE_EXT:
        try:
            import pandas as pd

            df = pd.read_csv(path) if ext == ".csv" else pd.read_excel(path)
            return Response(
                build_standalone_table_page(rel_workspace(path), df),
                mimetype="text/html; charset=utf-8",
            )
        except Exception as e:
            return Response(f"read failed: {e}", status=500)

    if ext in _TEXT_EXT:
        from html import escape

        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(200_000)
        return Response(
            "<!DOCTYPE html><meta charset='utf-8'><pre>"
            + escape(text)
            + "</pre>",
            mimetype="text/html; charset=utf-8",
        )

    return send_from_directory(directory, name)


app.index_string = """
<!DOCTYPE html>
<html>
<head>
{%metas%}
<title>喵喵agent</title>
{%favicon%}
{%css%}
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Nunito:wght@500;600;700;800&display=swap" rel="stylesheet">
<style>
  :root {
    --cat-cream: #FFF9F0;
    --cat-butter: #FFF1D6;
    --cat-milk: #FFFFFF;
    --cat-peach: #FFE2C4;
    --cat-ginger: #E8893A;
    --cat-ginger-deep: #D4722A;
    --cat-ginger-soft: #F6B26B;
    --cat-ink: #3B2A1C;
    --cat-ink-soft: #6B5344;
    --cat-line: #F0DCC0;
    --cat-shadow: rgba(212, 114, 42, 0.12);
  }
  html, body {
    height: 100%; margin: 0;
    font-family: "Nunito", "Segoe UI", sans-serif;
    color: var(--cat-ink);
    background:
      radial-gradient(1200px 600px at 10% -10%, #FFE8C8 0%, transparent 55%),
      radial-gradient(900px 500px at 100% 0%, #FFF6E8 0%, transparent 50%),
      var(--cat-cream);
  }
  .app-shell { display: flex; height: 100vh; min-height: 100vh; }
  .sidebar {
    width: 260px; flex-shrink: 0;
    background:
      url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='120' viewBox='0 0 120 120'%3E%3Cg fill='%23E8893A' fill-opacity='0.07'%3E%3Ccircle cx='40' cy='38' r='7'/%3E%3Ccircle cx='58' cy='28' r='6.5'/%3E%3Ccircle cx='76' cy='34' r='6.5'/%3E%3Ccircle cx='82' cy='52' r='6'/%3E%3Cellipse cx='58' cy='62' rx='16' ry='13'/%3E%3C/g%3E%3C/svg%3E"),
      linear-gradient(180deg, var(--cat-butter) 0%, #FFEAD0 100%);
    background-size: 120px 120px, auto;
    border-right: 1px solid var(--cat-line);
    padding: 16px 12px;
    overflow-y: auto;
  }
  .sidebar-brand {
    font-weight: 800; font-size: 1.12rem; margin-bottom: 12px; padding: 0 4px;
    color: var(--cat-ginger-deep);
    letter-spacing: 0.02em;
  }
  .sidebar-section-title {
    font-size: 0.75rem; color: var(--cat-ink-soft); text-transform: uppercase;
    letter-spacing: .04em; margin: 4px 4px 8px; font-weight: 700;
  }
  .sidebar-item { border: none !important; background: transparent !important; color: var(--cat-ink) !important; }
  .sidebar-item:hover { background: var(--cat-peach) !important; }
  .active-item { background: #FFD9A8 !important; box-shadow: inset 3px 0 0 var(--cat-ginger); }
  .main-pane {
    flex: 1; display: flex; flex-direction: column; min-width: 0;
    background: linear-gradient(180deg, var(--cat-milk) 0%, var(--cat-cream) 100%);
  }
  .main-header {
    padding: 14px 24px; border-bottom: 1px solid var(--cat-line);
    display: flex; justify-content: space-between; align-items: center;
    background: rgba(255,255,255,0.72);
    backdrop-filter: blur(6px);
  }
  .main-header strong { color: var(--cat-ink); font-weight: 800; }
  .main-header .text-muted { color: var(--cat-ink-soft) !important; }
  .chat-scroll {
    flex: 1; overflow-y: auto; padding: 24px 18% 16px;
  }
  @media (max-width: 960px) {
    .chat-scroll { padding: 16px 16px; }
    .sidebar { width: 220px; }
  }
  .chat-row { margin-bottom: 20px; }
  .chat-role { font-size: 0.78rem; color: var(--cat-ink-soft); margin-bottom: 6px; font-weight: 700; }
  .chat-bubble {
    word-break: break-word; line-height: 1.65;
    font-size: 0.98rem;
  }
  .user-bubble {
    white-space: pre-wrap;
    background: linear-gradient(135deg, #FFE8C8, #FFF3DF);
    border: 1px solid var(--cat-line);
    border-radius: 14px 14px 4px 14px; padding: 12px 14px;
    color: var(--cat-ink);
  }
  .ai-bubble { padding: 4px 2px; white-space: normal; color: var(--cat-ink); }
  .ai-bubble p { margin: 0 0 0.9em; }
  .ai-bubble p:last-child { margin-bottom: 0; }
  .ai-bubble strong { font-weight: 700; color: var(--cat-ginger-deep); }
  .artifact-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
  .artifact-chip {
    display: inline-block; padding: 6px 10px; border-radius: 999px;
    background: var(--cat-butter); border: 1px solid var(--cat-line);
    color: var(--cat-ginger-deep);
    text-decoration: none; font-size: 0.85rem; font-weight: 700;
  }
  .artifact-chip:hover { background: var(--cat-peach); color: var(--cat-ink); }
  .composer {
    border-top: 1px solid var(--cat-line); padding: 12px 18% 20px;
    background: rgba(255,249,240,0.85);
  }
  @media (max-width: 960px) { .composer { padding: 12px 16px 16px; } }
  .composer-box {
    border: 1px solid var(--cat-line); border-radius: 18px; background: var(--cat-milk);
    padding: 10px 12px 8px; box-shadow: 0 4px 16px var(--cat-shadow);
  }
  .composer-box .composer-input {
    border: none !important; box-shadow: none !important;
    padding: 6px 4px 10px !important; font-size: 0.98rem;
    background: transparent !important; color: var(--cat-ink) !important;
    resize: vertical; min-height: 52px; max-height: 160px;
    line-height: 1.45; width: 100%;
  }
  .composer-box .composer-input:focus {
    border: none !important; box-shadow: none !important;
  }
  .composer-box .composer-input::placeholder { color: #A8907A; }
  .composer-toolbar {
    display: flex; align-items: center; justify-content: space-between;
    gap: 8px; padding-top: 2px;
  }
  .composer-left { display: flex; align-items: center; gap: 8px; min-width: 0; flex-wrap: wrap; }
  .model-pill-wrap {
    min-width: 148px; max-width: 200px;
  }
  .thinking-btn {
    border-radius: 999px !important;
    min-height: 32px !important;
    padding: 2px 12px !important;
    font-size: 0.82rem !important;
    font-weight: 700 !important;
    border-color: var(--cat-line) !important;
    color: var(--cat-ink-soft) !important;
    background: var(--cat-butter) !important;
  }
  .thinking-btn:hover {
    border-color: var(--cat-ginger) !important;
    color: var(--cat-ink) !important;
  }
  .thinking-btn.on {
    background: linear-gradient(180deg, var(--cat-ginger-soft), var(--cat-ginger)) !important;
    border-color: var(--cat-ginger-deep) !important;
    color: #fff !important;
  }
  .model-pill-wrap .Select-control,
  .model-pill-wrap .css-1s2u09g-control,
  .model-pill-wrap .css-13cymwt-control {
    border-radius: 999px !important;
    border-color: var(--cat-line) !important;
    background: var(--cat-butter) !important;
    min-height: 32px !important;
    box-shadow: none !important;
  }
  .model-pill-wrap .Select-value-label,
  .model-pill-wrap .css-1dimb5e-singleValue {
    font-size: 0.82rem !important;
    color: var(--cat-ink) !important;
    font-weight: 700 !important;
  }
  .composer-actions { display: flex; align-items: center; gap: 8px; }
  .empty-hint { text-align: center; margin-top: 18vh; color: var(--cat-ink-soft); }
  .empty-hint h4 { color: var(--cat-ginger-deep); font-weight: 800; }
  .hist-row {
    position: relative; display: flex; align-items: stretch;
    margin-bottom: 4px; border-radius: 10px;
  }
  .hist-row:hover { background: var(--cat-peach); }
  .hist-row.active-item { background: #FFD9A8; box-shadow: inset 3px 0 0 var(--cat-ginger); }
  .hist-title { color: var(--cat-ink); font-weight: 700; }
  .hist-open-btn {
    flex: 1; border: none !important; background: transparent !important;
    box-shadow: none !important; padding: 8px 36px 8px 10px !important;
    color: var(--cat-ink) !important;
  }
  .hist-del-btn {
    position: absolute; right: 6px; top: 50%; transform: translateY(-50%);
    width: 28px; height: 28px; border: none; border-radius: 50%;
    background: transparent; color: #A8907A; cursor: pointer;
    opacity: 0; font-size: 0.9rem; line-height: 1;
  }
  .hist-row:hover .hist-del-btn { opacity: 1; }
  .hist-del-btn:hover { background: #FFE8E0; color: #C45C3E; }
  .text-muted { color: var(--cat-ink-soft) !important; }

  /* Bootstrap primary → 橘猫姜橙色 */
  .btn-primary {
    background: linear-gradient(180deg, var(--cat-ginger-soft), var(--cat-ginger)) !important;
    border-color: var(--cat-ginger-deep) !important;
    color: #fff !important;
    font-weight: 700 !important;
    box-shadow: 0 2px 8px var(--cat-shadow);
  }
  .btn-primary:hover, .btn-primary:focus {
    background: linear-gradient(180deg, var(--cat-ginger), var(--cat-ginger-deep)) !important;
    border-color: #B85F1F !important;
    color: #fff !important;
  }
  .btn-outline-danger {
    color: #C45C3E !important;
    border-color: #E8B4A4 !important;
  }
  .btn-outline-danger:hover {
    background: #FFE8E0 !important;
    color: #A94432 !important;
  }
  .btn-light {
    background: transparent !important;
    border: none !important;
    color: var(--cat-ink) !important;
  }
</style>
</head>
<body>
{%app_entry%}
<footer>{%config%}{%scripts%}{%renderer%}</footer>
</body>
</html>
"""

def serve_layout():
    """每次浏览器打开/刷新：归档上次草稿 → 空白新会话（不再「恢复进行中」）。"""
    rel = _archive_and_reset_to_blank(cancel_job=True)
    status = "就绪"
    if rel:
        status = "上次对话已归档到历史 · 新会话"
    return html.Div(
        [
            dcc.Store(id="history-tick", data=0),
            dcc.Store(id="composer-enter-bound", data=False),
            dcc.Store(id="pending-delete-file", data=None),
            dcc.Interval(id="agent-poll", interval=800, n_intervals=0, disabled=True),
            dbc.Modal(
                [
                    dbc.ModalHeader(dbc.ModalTitle("删除会话")),
                    dbc.ModalBody(
                        id="delete-modal-body",
                        children="确定删除这条历史记录吗？此操作不可恢复。",
                    ),
                    dbc.ModalFooter(
                        [
                            dbc.Button(
                                "取消",
                                id="btn-delete-cancel",
                                outline=True,
                                className="me-2",
                                n_clicks=0,
                            ),
                            dbc.Button(
                                "删除",
                                id="btn-delete-confirm",
                                color="danger",
                                n_clicks=0,
                            ),
                        ]
                    ),
                ],
                id="delete-modal",
                is_open=False,
                centered=True,
            ),
            html.Div(
                [
                    html.Div(
                        [
                            html.Div("仲町阿拉蕾", className="sidebar-brand"),
                            html.Div(
                                id="history-sidebar",
                                children=build_history_sidebar(),
                            ),
                        ],
                        className="sidebar",
                    ),
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.Strong("对话"),
                                    html.Span(
                                        id="agent-status",
                                        className="text-muted small",
                                        children=status,
                                    ),
                                ],
                                className="main-header",
                            ),
                            html.Div(
                                id="chat-box",
                                className="chat-scroll",
                                children=build_chat_ui([]),
                            ),
                            html.Div(
                                [
                                    html.Div(
                                        [
                                            dcc.Textarea(
                                                id="user-input",
                                                placeholder="描述你想要的画像或分析…（Enter 发送，Shift+Enter 换行）",
                                                value="",
                                                className="composer-input",
                                                style={"height": "56px"},
                                            ),
                                            html.Div(
                                                [
                                                    html.Div(
                                                        [
                                                            dcc.Dropdown(
                                                                id="model-select",
                                                                options=[
                                                                    {
                                                                        "label": f"{meta['label']} · {meta['hint']}",
                                                                        "value": mid,
                                                                    }
                                                                    for mid, meta in AVAILABLE_MODELS.items()
                                                                ],
                                                                value=normalize_model_id(None),
                                                                clearable=False,
                                                                searchable=False,
                                                                className="model-pill-wrap",
                                                            ),
                                                            dbc.Button(
                                                                "思考",
                                                                id="thinking-btn",
                                                                n_clicks=0,
                                                                size="sm",
                                                                color="secondary",
                                                                outline=True,
                                                                className="thinking-btn",
                                                                title="开启 DeepSeek 思考模式（更慢、更准，费更多 token）",
                                                            ),
                                                            dcc.Store(id="thinking-on", data=False),
                                                        ],
                                                        className="composer-left",
                                                    ),
                                                    html.Div(
                                                        [
                                                            dbc.Button(
                                                                "发送",
                                                                id="send-btn",
                                                                color="primary",
                                                                n_clicks=0,
                                                                size="sm",
                                                            ),
                                                            dbc.Button(
                                                                "中断",
                                                                id="stop-btn",
                                                                color="danger",
                                                                outline=True,
                                                                n_clicks=0,
                                                                disabled=True,
                                                                size="sm",
                                                            ),
                                                        ],
                                                        className="composer-actions",
                                                    ),
                                                ],
                                                className="composer-toolbar",
                                            ),
                                        ],
                                        className="composer-box",
                                    )
                                ],
                                className="composer",
                            ),
                        ],
                        className="main-pane",
                    ),
                ],
                className="app-shell",
            ),
        ]
    )


app.layout = serve_layout


# Enter 发送 / Shift+Enter 换行
app.clientside_callback(
    """
    function(tick, bound) {
        if (bound) {
            return window.dash_clientside.no_update;
        }
        document.addEventListener("keydown", function (e) {
            var el = e.target;
            if (!el || el.id !== "user-input") return;
            if (e.key !== "Enter" || e.shiftKey || e.isComposing) return;
            e.preventDefault();
            var btn = document.getElementById("send-btn");
            if (btn && !btn.disabled) btn.click();
        }, true);
        return true;
    }
    """,
    Output("composer-enter-bound", "data"),
    Input("history-tick", "data"),
    State("composer-enter-bound", "data"),
)


@app.callback(
    [
        Output("thinking-on", "data"),
        Output("thinking-btn", "className"),
        Output("thinking-btn", "children"),
    ],
    Input("thinking-btn", "n_clicks"),
    State("thinking-on", "data"),
    prevent_initial_call=True,
)
def toggle_thinking(n_clicks, on):
    if not n_clicks:
        return no_update, no_update, no_update
    nxt = not bool(on)
    cls = "thinking-btn on" if nxt else "thinking-btn"
    label = "思考 · 开" if nxt else "思考"
    return nxt, cls, label


@app.callback(
    Output("history-sidebar", "children"),
    Input("history-tick", "data"),
)
def refresh_history_sidebar(_tick):
    return build_history_sidebar()


@app.callback(
    [
        Output("chat-box", "children", allow_duplicate=True),
        Output("agent-status", "children", allow_duplicate=True),
        Output("history-tick", "data", allow_duplicate=True),
    ],
    Input("btn-new-session", "n_clicks"),
    State("history-tick", "data"),
    prevent_initial_call=True,
)
def new_session_click(n_clicks, tick):
    if not n_clicks:
        return no_update, no_update, no_update
    rel = _archive_and_reset_to_blank(cancel_job=True)
    tip = "已开新对话（空白）" + (f" · 旧草稿已归档 {rel}" if rel else "")
    return build_chat_ui([]), tip, (tick or 0) + 1


@app.callback(
    [
        Output("chat-box", "children", allow_duplicate=True),
        Output("agent-status", "children", allow_duplicate=True),
        Output("history-tick", "data", allow_duplicate=True),
    ],
    Input("btn-active-session", "n_clicks"),
    State("history-tick", "data"),
    prevent_initial_call=True,
)
def focus_active_draft(n_clicks, tick):
    global chat_history, conversation_summary, conversation_summary_covers
    if not n_clicks:
        return no_update, no_update, no_update
    with _job_lock:
        if _job["running"]:
            return no_update, "Agent 运行中，请先中断", no_update
        # 从历史浏览回到真实草稿（磁盘 _active），不要用正在看的历史覆盖
        if _nav.get("loaded_from"):
            chat_history, conversation_summary, conversation_summary_covers = (
                load_active_session(WORKSPACE_DIR)
            )
        _nav["loaded_from"] = None
        history = list(chat_history)
    return build_chat_ui(history), "当前进行中", (tick or 0) + 1


@app.callback(
    [
        Output("chat-box", "children", allow_duplicate=True),
        Output("agent-status", "children", allow_duplicate=True),
        Output("history-tick", "data", allow_duplicate=True),
    ],
    Input({"type": "hist-session", "file": ALL}, "n_clicks"),
    State("history-tick", "data"),
    prevent_initial_call=True,
)
def open_history_session(n_clicks, tick):
    """点历史 = 只切换显示，不写 _active、不 archive（否则会覆盖草稿并复制出相同会话）。"""
    global chat_history, conversation_summary, conversation_summary_covers
    if not isinstance(ctx.triggered_id, dict):
        return no_update, no_update, no_update
    if ctx.triggered_id.get("type") != "hist-session":
        return no_update, no_update, no_update
    if not any(n_clicks or []):
        return no_update, no_update, no_update

    filename = ctx.triggered_id["file"]
    if filename == _nav.get("loaded_from"):
        return no_update, no_update, no_update

    with _job_lock:
        if _job["running"]:
            return no_update, "Agent 运行中，请先中断", no_update
        history, summary, topic, covers = load_archived_session(WORKSPACE_DIR, filename)
        if not history and not summary:
            return no_update, f"无法打开：{filename}", no_update
        # 若当前是草稿，先把草稿落盘，再只改内存用于浏览
        if not _nav.get("loaded_from"):
            _persist_active_unlocked()
        chat_history = history
        conversation_summary = summary
        conversation_summary_covers = covers
        _nav["loaded_from"] = filename
        # 故意不写 _active：草稿仍保留在磁盘，刷新仍恢复草稿而非历史副本
    return (
        build_chat_ui(history),
        f"已打开历史：{topic}",
        (tick or 0) + 1,
    )


@app.callback(
    [
        Output("delete-modal", "is_open"),
        Output("pending-delete-file", "data"),
        Output("delete-modal-body", "children"),
    ],
    Input({"type": "hist-del", "file": ALL}, "n_clicks"),
    prevent_initial_call=True,
)
def ask_delete_history(n_clicks):
    if not isinstance(ctx.triggered_id, dict):
        return no_update, no_update, no_update
    if ctx.triggered_id.get("type") != "hist-del":
        return no_update, no_update, no_update
    if not any(n_clicks or []):
        return no_update, no_update, no_update
    filename = ctx.triggered_id["file"]
    topic = "该会话"
    for s in list_archived_sessions(WORKSPACE_DIR):
        if s["filename"] == filename:
            topic = s.get("topic") or topic
            break
    return True, filename, f"确定删除「{topic}」吗？此操作不可恢复。"


@app.callback(
    [
        Output("delete-modal", "is_open", allow_duplicate=True),
        Output("pending-delete-file", "data", allow_duplicate=True),
        Output("history-tick", "data", allow_duplicate=True),
        Output("chat-box", "children", allow_duplicate=True),
        Output("agent-status", "children", allow_duplicate=True),
    ],
    [
        Input("btn-delete-cancel", "n_clicks"),
        Input("btn-delete-confirm", "n_clicks"),
    ],
    [
        State("pending-delete-file", "data"),
        State("history-tick", "data"),
    ],
    prevent_initial_call=True,
)
def resolve_delete_history(n_cancel, n_confirm, pending, tick):
    global chat_history, conversation_summary, conversation_summary_covers
    tid = ctx.triggered_id
    if tid == "btn-delete-cancel":
        return False, None, no_update, no_update, no_update
    if tid != "btn-delete-confirm" or not pending:
        return no_update, no_update, no_update, no_update, no_update

    with _job_lock:
        if _job["running"]:
            return False, None, no_update, no_update, "Agent 运行中，请先中断"
        ok = delete_archived_session(WORKSPACE_DIR, pending)
        chat_out = no_update
        status = "已取消删除" if not ok else "已删除会话"
        if ok and _nav.get("loaded_from") == pending:
            chat_history = []
            conversation_summary = ""
            conversation_summary_covers = 0
            clear_active_session(WORKSPACE_DIR)
            _nav["loaded_from"] = None
            chat_out = build_chat_ui([])
            status = "已删除当前打开的历史会话"
        elif not ok:
            status = "删除失败（文件不存在或无权删除）"
    return False, None, (tick or 0) + 1, chat_out, status


@app.callback(
    [
        Output("chat-box", "children", allow_duplicate=True),
        Output("user-input", "value"),
        Output("agent-status", "children", allow_duplicate=True),
        Output("agent-poll", "disabled", allow_duplicate=True),
        Output("send-btn", "disabled", allow_duplicate=True),
        Output("stop-btn", "disabled", allow_duplicate=True),
        Output("model-select", "disabled", allow_duplicate=True),
        Output("thinking-btn", "disabled", allow_duplicate=True),
        Output("history-tick", "data", allow_duplicate=True),
    ],
    [Input("send-btn", "n_clicks")],
    [
        State("user-input", "value"),
        State("model-select", "value"),
        State("thinking-on", "data"),
        State("history-tick", "data"),
    ],
    prevent_initial_call=True,
)
def start_chat(n_clicks, user_text, model_id, thinking_on, tick):
    global chat_history, conversation_summary
    if not user_text:
        return (no_update,) * 9

    mid = normalize_model_id(model_id)
    think = bool(thinking_on)
    status_extra = ""
    tick_out = no_update

    if wants_new_session(user_text):
        rel = _archive_and_reset_to_blank(cancel_job=True)
        status_extra = f"已归档 {rel}。" if rel else "已清空。"
        tick_out = (tick or 0) + 1
        user_text = strip_clear_keywords(user_text)
        if not user_text:
            return (
                build_chat_ui([]),
                "",
                f"新对话已开始。{status_extra}",
                True,
                False,
                True,
                False,
                False,
                tick_out,
            )

    with _job_lock:
        if _job["running"]:
            return (
                no_update,
                no_update,
                "仍在处理上一轮，可点中断",
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
            )
        _job["running"] = True
        _job["error"] = None
        _job["model_id"] = mid
        _job["thinking"] = think
        # 在历史浏览中发消息 = 从该历史分叉成新草稿
        if _nav.get("loaded_from"):
            _nav["loaded_from"] = None
        chat_history = list(chat_history) + [{"role": "user", "content": user_text}]
        snapshot = list(chat_history)
        epoch = int(_nav.get("session_epoch") or 0)
        _persist_active_unlocked()

    threading.Thread(
        target=_run_agent_job, args=(snapshot, mid, epoch, think), daemon=True
    ).start()
    waiting = html.Div("思考中…" if think else "处理中…", className="text-muted mb-3")
    status = f"运行中 · {model_label(mid, thinking=think)}"
    if status_extra:
        status = status_extra + " " + status
    return (
        build_chat_ui(snapshot, trailing=waiting),
        "",
        status,
        False,
        True,
        False,
        True,
        True,
        tick_out,
    )


@app.callback(
    Output("agent-status", "children", allow_duplicate=True),
    Input("stop-btn", "n_clicks"),
    prevent_initial_call=True,
)
def stop_agent(n_clicks):
    if not n_clicks:
        return no_update
    with _job_lock:
        if not _job["running"]:
            return "当前没有运行中的任务"
        _job["cancel"].set()
    request_kill_sandbox()
    return "正在中断…"


@app.callback(
    [
        Output("chat-box", "children"),
        Output("agent-status", "children"),
        Output("agent-poll", "disabled"),
        Output("send-btn", "disabled"),
        Output("stop-btn", "disabled"),
        Output("model-select", "disabled"),
        Output("thinking-btn", "disabled"),
        Output("history-tick", "data"),
    ],
    Input("agent-poll", "n_intervals"),
    State("history-tick", "data"),
    prevent_initial_call=True,
)
def poll_agent(_n, tick):
    with _job_lock:
        running = _job["running"]
        history = list(chat_history)
        err = _job.get("error")
        mid = _job.get("model_id")
        think = bool(_job.get("thinking"))

    if running:
        label = model_label(mid, thinking=think) if mid else ""
        status = f"运行中 · {label}" if label else "运行中"
        wait = "思考中…" if think else "处理中…"
        return (
            build_chat_ui(history, trailing=html.Div(wait, className="text-muted mb-3")),
            status,
            False,
            True,
            False,
            True,
            True,
            no_update,
        )

    status = "完成"
    if err == "cancelled":
        status = "已中断"
    elif err:
        status = f"异常：{err}"
    return build_chat_ui(history), status, True, False, True, False, False, (tick or 0) + 1


if __name__ == "__main__":
    app.run(debug=True, port=8051, threaded=True, use_reloader=False)
