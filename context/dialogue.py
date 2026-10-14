"""上下文治理：模型摘要（方案 B）+ 会话归档。"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any

KEEP_RECENT_USER_TURNS = 4  # 保留窗口 Keep：摘要触发后留下的原文用户轮次数
MAX_RAW_USER_TURNS = 8  # 触发阈值 Max：相对水位线的原文用户轮次超过此值才摘要
# 兼容旧常量名（曾与 Keep 几乎重合，导致过线后每轮都摘要）
SUMMARIZE_AFTER_USER_TURNS = MAX_RAW_USER_TURNS

CLEAR_KEYWORDS = ("新任务", "清空上下文", "重新开始", "新会话")


def _role(msg: Any) -> str:
    if isinstance(msg, dict):
        return msg.get("role") or msg.get("type") or ""
    return getattr(msg, "type", "") or ""


def _content(msg: Any) -> str:
    if isinstance(msg, dict):
        return msg.get("content") or ""
    c = getattr(msg, "content", "") or ""
    return c if isinstance(c, str) else str(c)


def _tool_calls(msg: Any):
    if isinstance(msg, dict):
        return msg.get("tool_calls")
    return getattr(msg, "tool_calls", None) or None


def _is_user(msg: Any) -> bool:
    return _role(msg) in ("user", "human")


def _is_ai(msg: Any) -> bool:
    return _role(msg) in ("assistant", "ai")


def _is_tool(msg: Any) -> bool:
    return _role(msg) in ("tool",)


def to_wire_message(msg: Any) -> dict:
    if _is_user(msg):
        return {"role": "user", "content": _content(msg)}
    if _is_ai(msg):
        return {"role": "assistant", "content": _content(msg)}
    return {"role": "user", "content": _content(msg)}


def extract_visible_dialogue(history: list) -> list:
    """只保留用户话 + 最终助手回复（去掉 tool 轨迹）。"""
    visible = []
    for msg in history:
        if _is_tool(msg):
            continue
        if _is_ai(msg) and _tool_calls(msg):
            continue
        if _is_user(msg) or (_is_ai(msg) and _content(msg).strip()):
            visible.append(msg)
    return visible


def _format_visible_for_summary(msgs: list) -> str:
    lines = []
    for msg in msgs:
        who = "用户" if _is_user(msg) else "助手"
        text = _content(msg).strip()
        if len(text) > 1200:
            text = text[:1200] + "…(截断)"
        lines.append(f"{who}: {text}")
    return "\n".join(lines)


def summarize_with_llm(llm, older_text: str, prev_summary: str = "") -> str:
    """方案 B：再调一次模型，把旧对话压成短摘要。"""
    prompt = (
        "你是会话摘要器。请用中文输出不超过 10 条要点，覆盖："
        "用户目标、已做操作、生成的文件/路径、关键结论与未完成事项。"
        "若涉及白名单会话整理：保留信息卡要点、说话人、写表/日程意向；"
        "注明「原文在会话缓存，追问用 query_cached_chat」，不要把群聊全文写进摘要。"
        "不要废话，不要编号以外的开场白。\n\n"
    )
    if prev_summary.strip():
        prompt += f"【已有摘要】\n{prev_summary.strip()}\n\n"
    prompt += f"【需合并的更早对话】\n{older_text}\n"

    try:
        resp = llm.invoke(prompt)
        text = getattr(resp, "content", None) or str(resp)
        if isinstance(text, list):
            text = "".join(str(x) for x in text)
        return (text or "").strip() or prev_summary
    except Exception as e:
        fallback = prev_summary.strip()
        note = f"(摘要失败: {e})"
        return f"{fallback}\n{note}".strip() if fallback else note


def build_model_context(
    full_history: list,
    summary: str,
    llm,
    keep_recent: int = KEEP_RECENT_USER_TURNS,
    max_raw: int = MAX_RAW_USER_TURNS,
    summary_covers: int = 0,
) -> tuple[list, str, int]:
    """
    弹性滑动窗口：构造发给 Agent 的瘦身 messages，并可能更新 summary。

    - 静默期：相对水位线的原文用户轮次 ≤ Max → 不调摘要 LLM
    - 触发期：原文用户轮次 > Max → 只把「未覆盖且不在 Keep 内」的那段并入摘要，
      水位线推进到 n_user - Keep；之后再静默增长，避免每轮重压

    返回 (model_messages, new_summary, new_summary_covers)。
    summary_covers = 摘要已覆盖的「可见对话中的用户轮次数」（从最早一轮起算）。
    """
    visible = extract_visible_dialogue(full_history)
    if not visible:
        return [], summary or "", max(0, int(summary_covers or 0))

    user_positions = [i for i, m in enumerate(visible) if _is_user(m)]
    n_user = len(user_positions)
    new_summary = summary or ""
    covered = max(0, int(summary_covers or 0))

    # 旧会话无水位线：假定摘要已覆盖「除最近 Keep 外」的轮次，避免升级后每轮全量重压
    if covered <= 0 and new_summary.strip() and n_user > keep_recent:
        covered = n_user - keep_recent
    covered = min(covered, n_user)

    raw_count = n_user - covered

    if raw_count > max_raw and n_user > keep_recent:
        split_at = user_positions[-keep_recent]
        older_start = user_positions[covered] if covered > 0 else 0
        older = visible[older_start:split_at]
        recent = visible[split_at:]
        if older:
            older_text = _format_visible_for_summary(older)
            new_summary = summarize_with_llm(llm, older_text, new_summary)
        covered = n_user - keep_recent
    elif covered > 0 and covered < n_user:
        recent = visible[user_positions[covered] :]
    else:
        recent = visible

    model_msgs: list = []
    if new_summary.strip():
        model_msgs.append({
            "role": "user",
            "content": (
                "[会话摘要 — 更早对话的压缩记忆，请结合后续消息继续任务]\n"
                + new_summary.strip()
            ),
        })
        model_msgs.append({
            "role": "assistant",
            "content": "好的，我已记住摘要中的背景，请继续。",
        })

    model_msgs.extend(to_wire_message(m) for m in recent)
    return model_msgs, new_summary, covered


def wants_new_session(text: str) -> bool:
    return any(k in (text or "") for k in CLEAR_KEYWORDS)


def strip_clear_keywords(text: str) -> str:
    rest = text or ""
    for k in CLEAR_KEYWORDS:
        rest = rest.replace(k, "")
    return rest.strip(" \t\n\r：:，,.-")


# 会话侧边栏标题字数（Python len：中文/英文均按 1 字符计）
# 可在 .env 调整，例如 SESSION_TITLE_MAX_CHARS=28
SESSION_TITLE_MIN_CHARS = 4
SESSION_TITLE_MAX_CHARS = 28


def _title_limits() -> tuple[int, int]:
    """允许用环境变量覆盖标题长度。"""
    try:
        lo = int(os.getenv("SESSION_TITLE_MIN_CHARS", str(SESSION_TITLE_MIN_CHARS)))
    except ValueError:
        lo = SESSION_TITLE_MIN_CHARS
    try:
        hi = int(os.getenv("SESSION_TITLE_MAX_CHARS", str(SESSION_TITLE_MAX_CHARS)))
    except ValueError:
        hi = SESSION_TITLE_MAX_CHARS
    lo = max(2, min(lo, 48))
    hi = max(lo, min(hi, 48))
    return lo, hi


def _sanitize_topic(topic: str, max_chars: int | None = None) -> str:
    """侧边栏展示用短标题：保留空格与中文，去掉路径危险字符。"""
    _, hi = _title_limits()
    limit = max_chars if max_chars is not None else hi
    topic = (topic or "").strip()
    topic = re.sub(r"^[\"'「」『』《》]+|[\"'「」『』《》。．!！?？:：;；]+$", "", topic)
    topic = re.sub(r'[\\/:*?"<>|]+', "", topic)
    topic = re.sub(r"\s+", " ", topic).strip()
    if len(topic) > limit:
        topic = topic[:limit].rstrip()
    return topic or "未命名"


def topic_to_filename_slug(topic: str) -> str:
    """仅用于归档文件名。"""
    slug = re.sub(r'[\\/:*?"<>|\s]+', "_", topic or "")
    slug = re.sub(r"_+", "_", slug).strip("._")
    return slug[:40] or "未命名"


def _is_bad_title(title: str) -> bool:
    """过滤叙事体截断标题（如「用户先询问…随后」）。"""
    t = (title or "").strip()
    if not t:
        return True
    bad_starts = ("用户", "助手", "把下面", "根据", "请", "我")
    bad_mids = ("随后", "然后", "接着", "以及询问", "先询问", "总结如下")
    if any(t.startswith(p) for p in bad_starts) and len(t) > 8:
        if any(x in t for x in ("询问", "表示", "希望", "想要", "说")):
            return True
    if any(x in t for x in bad_mids):
        return True
    if t.endswith(("随后", "然后", "以及", "等等", "…", "...")):
        return True
    return False


def _collect_title_seed(history: list, summary: str) -> str:
    """多轮用户话 + 摘要片段，给标题模型更全的上下文。"""
    parts: list[str] = []
    if (summary or "").strip():
        # 摘要前几行即可，避免叙事第一句直接当标题
        lines = [ln.strip() for ln in summary.strip().splitlines() if ln.strip()]
        parts.append("摘要线索: " + "；".join(lines[:3])[:280])
    user_bits = []
    for msg in history:
        if _is_user(msg):
            text = _content(msg).strip().splitlines()[0]
            if text:
                user_bits.append(text[:120])
    if user_bits:
        # 首条 + 最近两条，贴近 Gemini「整段对话主题」
        picked = [user_bits[0]]
        if len(user_bits) > 1:
            picked.extend(user_bits[-2:])
        # 去重保序
        seen = set()
        uniq = []
        for u in picked:
            if u not in seen:
                seen.add(u)
                uniq.append(u)
        parts.append("用户消息: " + " | ".join(uniq))
    return "\n".join(parts)


def derive_session_topic(history: list, summary: str, llm=None) -> str:
    """
    Gemini 风格侧边栏短标题：名词/动宾短语，不是截断原句。
    字数由 SESSION_TITLE_MIN/MAX_CHARS（或模块默认）控制。
    """
    lo, hi = _title_limits()
    seed = _collect_title_seed(history, summary)
    if not seed.strip():
        return "未命名"

    if llm is not None:
        try:
            prompt = (
                "你是聊天侧边栏标题生成器（风格类似 Gemini Recents）。\n"
                "根据对话线索写一个短标题，硬性要求：\n"
                f"1. 【字数硬限制】标题总长度必须在 {lo}～{hi} 个字符以内（含英文；"
                f"Steam、Spotify 每个字母都算 1 个字符）。宁可缩短措辞，"
                f"也禁止超出 {hi}，禁止写到一半被截断（如「数据融」缺「合」）。\n"
                "2. 用完整主题短语，例如「Steam与Spotify数据融合」「Steam游玩画像」；\n"
                "3. 禁止叙事句，例如「用户先询问…随后…」；\n"
                "4. 不要引号、书名号、编号、末尾标点；只输出一行标题，不要解释。\n\n"
                f"【对话线索】\n{seed[:900]}\n"
            )
            resp = llm.invoke(prompt)
            text = getattr(resp, "content", None) or str(resp)
            if isinstance(text, list):
                text = "".join(str(x) for x in text)
            text = (text or "").strip().splitlines()[0].strip()
            text = re.sub(r"^[\"'「」『』《》]+|[\"'「」『』《》。．!！?？:：;；]+$", "", text)
            if text and not _is_bad_title(text):
                return _sanitize_topic(text, max_chars=hi)
        except Exception:
            pass

    # 无模型或失败：从首条用户话抽关键词式短截，避免「随后」半句
    first_user = ""
    for msg in history:
        if _is_user(msg):
            first_user = _content(msg).strip().splitlines()[0]
            break
    fallback = first_user or seed.splitlines()[-1]
    fallback = re.sub(r"^(请|帮我|我想|我要)+", "", fallback)
    fallback = fallback[:hi]
    if _is_bad_title(fallback):
        fallback = fallback[: max(lo, min(8, hi))]
    return _sanitize_topic(fallback, max_chars=hi)

def archive_session(
    history: list,
    summary: str,
    workspace_dir: str,
    llm=None,
    summary_covers: int = 0,
) -> str | None:
    """把当前会话落到 workspace/_sessions/session_时间_主题.json。"""
    if not history and not (summary or "").strip():
        return None

    sessions_dir = os.path.join(workspace_dir, "_sessions")
    os.makedirs(sessions_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    topic = derive_session_topic(history, summary, llm=llm)
    slug = topic_to_filename_slug(topic)
    filename = f"session_{ts}_{slug}.json"
    path = os.path.join(sessions_dir, filename)

    serializable = []
    for msg in history:
        item = {"role": _role(msg), "content": _content(msg)}
        tc = _tool_calls(msg)
        if tc:
            item["tool_calls"] = str(tc)[:2000]
        serializable.append(item)

    payload = {
        "archived_at": ts,
        "topic": topic,
        "summary": summary or "",
        "summary_covers_user_turns": max(0, int(summary_covers or 0)),
        "message_count": len(history),
        "messages": serializable,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    return f"_sessions/{filename}"


ACTIVE_SESSION_FILE = "_active.json"

# 归档列表缓存：目录文件名+mtime 未变则跳过解析大 JSON（侧栏/Agent 常扫）
_archive_list_cache: dict[str, Any] = {"sig": None, "items": []}


def _active_path(workspace_dir: str) -> str:
    sessions_dir = os.path.join(workspace_dir, "_sessions")
    os.makedirs(sessions_dir, exist_ok=True)
    return os.path.join(sessions_dir, ACTIVE_SESSION_FILE)


def _serialize_history(history: list) -> list:
    serializable = []
    for msg in history:
        item = {"role": _role(msg), "content": _content(msg)}
        tc = _tool_calls(msg)
        if tc:
            # 保留结构便于恢复；过长则截断字符串
            try:
                item["tool_calls"] = tc
            except Exception:
                item["tool_calls"] = str(tc)[:2000]
        serializable.append(item)
    return serializable


def save_active_session(
    history: list,
    summary: str,
    workspace_dir: str,
    summary_covers: int = 0,
) -> None:
    """进行中草稿落盘（防进程崩溃）。浏览器刷新由 UI 自动归档到历史后再开空白会话。"""
    path = _active_path(workspace_dir)
    payload = {
        "summary": summary or "",
        "summary_covers_user_turns": max(0, int(summary_covers or 0)),
        "message_count": len(history),
        "messages": _serialize_history(history),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def load_active_session(workspace_dir: str) -> tuple[list, str, int]:
    """读取进行中草稿（点「进行中」或刷新归档前）→ (history, summary, summary_covers)。"""
    path = _active_path(workspace_dir)
    if not os.path.isfile(path):
        return [], "", 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        messages = data.get("messages") or []
        summary = data.get("summary") or ""
        covers = int(data.get("summary_covers_user_turns") or 0)
        # 统一成 dict，后续逻辑已兼容
        history = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            history.append(m)
        return history, summary, covers
    except Exception:
        return [], "", 0


def clear_active_session(workspace_dir: str) -> None:
    path = _active_path(workspace_dir)
    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass


def delete_archived_session(workspace_dir: str, filename: str) -> bool:
    """删除一条归档会话文件。成功返回 True。"""
    safe = os.path.basename(filename or "")
    if not safe.startswith("session_") or not safe.endswith(".json"):
        return False
    path = os.path.join(workspace_dir, "_sessions", safe)
    if not os.path.isfile(path):
        return False
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def list_archived_sessions(workspace_dir: str, limit: int = 40) -> list[dict]:
    """列出已归档会话（供左侧历史栏 / Agent 工具）。不含 _active.json。"""
    sessions_dir = os.path.join(workspace_dir, "_sessions")
    if not os.path.isdir(sessions_dir):
        return []
    try:
        names = [
            n
            for n in os.listdir(sessions_dir)
            if n.startswith("session_") and n.endswith(".json")
        ]
        sig = tuple(
            sorted(
                (n, os.path.getmtime(os.path.join(sessions_dir, n))) for n in names
            )
        )
    except OSError:
        return []

    cached_sig = _archive_list_cache.get("sig")
    cached_items = _archive_list_cache.get("items")
    if cached_sig == (workspace_dir, sig) and isinstance(cached_items, list):
        return cached_items[: max(0, int(limit or 40))]

    items = []
    for name in names:
        path = os.path.join(sessions_dir, name)
        topic = "未命名"
        archived_at = ""
        message_count = 0
        has_summary = False
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            topic = data.get("topic") or topic
            archived_at = data.get("archived_at") or ""
            message_count = int(data.get("message_count") or len(data.get("messages") or []))
            has_summary = bool((data.get("summary") or "").strip())
        except Exception:
            # 文件名 session_YYYYMMDD_HHMMSS_主题.json
            parts = name[len("session_") : -len(".json")].split("_", 2)
            if len(parts) >= 3:
                archived_at = f"{parts[0]}_{parts[1]}"
                topic = parts[2]
            elif len(parts) == 2:
                archived_at = f"{parts[0]}_{parts[1]}"
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0
        items.append(
            {
                "filename": name,
                "topic": topic,
                "archived_at": archived_at,
                "message_count": message_count,
                "has_summary": has_summary,
                "mtime": mtime,
            }
        )
    items.sort(key=lambda x: x["mtime"], reverse=True)
    _archive_list_cache["sig"] = (workspace_dir, sig)
    _archive_list_cache["items"] = items
    return items[: max(0, int(limit or 40))]


def resolve_archived_filename(
    workspace_dir: str,
    *,
    filename: str = "",
    topic_query: str = "",
) -> str | None:
    """按文件名或主题关键字解析归档；多命中时取最近一条。"""
    safe = os.path.basename((filename or "").strip())
    if safe.startswith("session_") and safe.endswith(".json"):
        path = os.path.join(workspace_dir, "_sessions", safe)
        if os.path.isfile(path):
            return safe
    q = (topic_query or "").strip().lower()
    if not q:
        return None
    hits = [
        s
        for s in list_archived_sessions(workspace_dir, limit=80)
        if q in (s.get("topic") or "").lower() or q in (s.get("filename") or "").lower()
    ]
    if not hits:
        return None
    return hits[0]["filename"]


def archived_session_for_agent(
    workspace_dir: str,
    *,
    filename: str = "",
    topic_query: str = "",
    max_messages: int = 24,
    content_chars: int = 800,
) -> dict[str, Any]:
    """给 Agent 的瘦身归档：可见对话（无 tool 轨迹）+ 摘要。"""
    resolved = resolve_archived_filename(
        workspace_dir, filename=filename, topic_query=topic_query
    )
    if not resolved:
        return {
            "ok": False,
            "error": "archive_not_found",
            "hint": "先 list_chat_archives；再用 filename 或 topic_query。",
        }
    history, summary, topic, _covers = load_archived_session(workspace_dir, resolved)
    visible = extract_visible_dialogue(history)
    cap = max(4, min(int(max_messages or 24), 60))
    char_cap = max(200, min(int(content_chars or 800), 2000))
    trimmed = visible[-cap:]
    dialogue = []
    for m in trimmed:
        text = _content(m).strip()
        if len(text) > char_cap:
            text = text[:char_cap] + "…(截断)"
        dialogue.append(
            {
                "role": "user" if _is_user(m) else "assistant",
                "content": text,
            }
        )
    return {
        "ok": True,
        "filename": resolved,
        "topic": topic,
        "summary": (summary or "").strip(),
        "message_count_total": len(history),
        "visible_returned": len(dialogue),
        "truncated": len(visible) > len(dialogue),
        "dialogue": dialogue,
        "note": "只读归档；不自动并入当前会话。继续任务请在本会话复述要点或让用户点开左侧历史。",
    }


def load_archived_session(
    workspace_dir: str, filename: str
) -> tuple[list, str, str, int]:
    """加载归档会话 → (history, summary, topic, summary_covers)。"""
    safe = os.path.basename(filename or "")
    if not safe.startswith("session_") or not safe.endswith(".json"):
        return [], "", "", 0
    path = os.path.join(workspace_dir, "_sessions", safe)
    if not os.path.isfile(path):
        return [], "", "", 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        history = [m for m in (data.get("messages") or []) if isinstance(m, dict)]
        summary = data.get("summary") or ""
        topic = data.get("topic") or "未命名"
        covers = int(data.get("summary_covers_user_turns") or 0)
        return history, summary, topic, covers
    except Exception:
        return [], "", "", 0
