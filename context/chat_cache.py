"""
白名单会话 · 方案一：会话级 ephemeral 缓存 + 按需检索。

- 全量消息存在进程内存（按 session_key 隔离），不塞进 LLM 历史滚雪球
- 上下文只留「索引钥匙」；追问细节用 query_cached_chat
- 检索：字面 → 同义扩展 → 字符 bigram 模糊（缓存=时间窗内原文；条数不以「省内存」为产品帽）
- TTL：默认 1h，访问/写入时清理过期，防浏览器直关泄漏
- 单机 Dash 演示用内存；多进程需换 Redis（接口可保持）
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any

_lock = threading.Lock()
_active_session_key: str = "default"
# session_key -> {updated, ttl_sec, hours, whitelist, speakers, messages, ...}
_store: dict[str, dict[str, Any]] = {}

DEFAULT_TTL_SEC = 3600  # 1 小时

# 领域同义簇：缓解「用户说周报、原话说表格」类字面漏检（非真·向量语义）
_SYNONYM_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"周报", "表格", "计划表", "多维表", "表", "字段", "表头", "规范", "格式"}),
    frozenset({"评审", "开会", "会议", "对齐", "同步会", "项目评审"}),
    frozenset({"日历", "日程", "排期", "约会", "会议邀约"}),
    frozenset({"待办", "todo", "任务", "事项", "行动项"}),
    frozenset({"演示", "demo", "走通", "验收"}),
)


def set_active_session_key(key: str | None) -> None:
    global _active_session_key
    k = (key or "").strip() or "default"
    with _lock:
        _active_session_key = k


def get_active_session_key() -> str:
    with _lock:
        return _active_session_key


def _purge_expired_unlocked(now: float | None = None) -> int:
    """持锁调用：删过期缓存。"""
    ts = time.time() if now is None else now
    dead = []
    for k, entry in _store.items():
        ttl = float(entry.get("ttl_sec") or DEFAULT_TTL_SEC)
        updated = float(entry.get("updated") or 0)
        if updated <= 0 or (ts - updated) > ttl:
            dead.append(k)
    for k in dead:
        _store.pop(k, None)
    return len(dead)


def clear_session_cache(session_key: str | None = None) -> None:
    key = (session_key or "").strip() or get_active_session_key()
    with _lock:
        _store.pop(key, None)


def store_info_cards(
    cards: list[dict[str, Any]],
    *,
    session_key: str | None = None,
    extract_meta: dict[str, Any] | None = None,
) -> None:
    """把旁路提取的信息卡挂到当前会话缓存（与原文并存）。"""
    key = (session_key or "").strip() or get_active_session_key()
    with _lock:
        _purge_expired_unlocked()
        entry = _store.get(key)
        if not entry:
            return
        entry["info_cards"] = list(cards or [])
        entry["extract_meta"] = extract_meta or {}
        entry["updated"] = time.time()


def get_cached_messages(session_key: str | None = None) -> list[dict[str, Any]]:
    key = (session_key or "").strip() or get_active_session_key()
    with _lock:
        _purge_expired_unlocked()
        entry = _store.get(key)
        if not entry:
            return []
        return list(entry.get("messages") or [])


def get_cached_info_cards(session_key: str | None = None) -> list[dict[str, Any]]:
    key = (session_key or "").strip() or get_active_session_key()
    with _lock:
        entry = _store.get(key)
        if not entry:
            return []
        return list(entry.get("info_cards") or [])


def clear_all_chat_caches() -> None:
    with _lock:
        _store.clear()


def store_chat_digest(
    payload: dict[str, Any],
    *,
    session_key: str | None = None,
    ttl_sec: int | None = None,
) -> dict[str, Any]:
    """把 digest 的 messages 写入当前会话缓存，返回索引摘要。"""
    if not isinstance(payload, dict):
        return {"cached": False, "reason": "invalid payload"}
    key = (session_key or "").strip() or get_active_session_key()
    speakers: dict[str, str] = {}
    messages: list[dict[str, Any]] = []
    for chat in payload.get("chats") or []:
        if not isinstance(chat, dict):
            continue
        cid = chat.get("chat_id")
        for m in chat.get("messages") or []:
            if not isinstance(m, dict):
                continue
            text = (m.get("text") or "").strip()
            if not text:
                continue
            sid = str(m.get("sender_id") or "").strip()
            sname = (m.get("sender_name") or "").strip() or sid or "未知"
            if sid:
                speakers[sid] = sname
            messages.append(
                {
                    "chat_id": cid,
                    "message_id": m.get("message_id"),
                    "sender_id": sid or None,
                    "speaker": sname,
                    "time": m.get("create_time"),
                    "text": text[:2000],
                }
            )
    ttl = int(ttl_sec) if ttl_sec is not None else DEFAULT_TTL_SEC
    ttl = max(60, min(ttl, 86400))
    entry = {
        "updated": time.time(),
        "ttl_sec": ttl,
        "hours": payload.get("hours"),
        "range_start": payload.get("range_start"),
        "range_end": payload.get("range_end"),
        "max_messages": payload.get("max_messages"),
        "recent_n": payload.get("recent_n"),
        "ingest_policy": payload.get("ingest_policy"),
        "whitelist": payload.get("whitelist"),
        "source": payload.get("source"),
        "speakers": speakers,
        # 约定：messages[0] 最新（飞书 ByCreateTimeDesc）
        "messages": messages,
        "message_count": len(messages),
        "truncated": any(
            bool(c.get("truncated")) for c in (payload.get("chats") or []) if isinstance(c, dict)
        ),
    }
    with _lock:
        _purge_expired_unlocked()
        _store[key] = entry
    return cache_index(session_key=key)


def cache_index(session_key: str | None = None) -> dict[str, Any]:
    key = (session_key or "").strip() or get_active_session_key()
    with _lock:
        _purge_expired_unlocked()
        entry = _store.get(key)
        if not entry:
            return {"cached": False, "session_key": key}
        speakers = entry.get("speakers") or {}
        names = sorted({str(v) for v in speakers.values() if v})
        ttl = float(entry.get("ttl_sec") or DEFAULT_TTL_SEC)
        age = time.time() - float(entry.get("updated") or 0)
        return {
            "cached": True,
            "session_key": key,
            "message_count": int(entry.get("message_count") or 0),
            "info_card_count": len(entry.get("info_cards") or []),
            "speakers": names,
            "speaker_ids": list(speakers.keys()),
            "hours": entry.get("hours"),
            "recent_n": entry.get("recent_n"),
            "range_start": entry.get("range_start"),
            "range_end": entry.get("range_end"),
            "max_messages": entry.get("max_messages"),
            "truncated": bool(entry.get("truncated")),
            "whitelist": entry.get("whitelist"),
            "ttl_sec": ttl,
            "expires_in_sec": max(0, int(ttl - age)),
            "hint": (
                "Raw msgs in session cache; quotes via query_cached_chat. "
                "If user changes hours/recent_n or asks to re-pull, call fetch again."
            ),
        }


def _parse_keyword_seeds(keyword: str) -> list[str]:
    """支持逗号/分号/竖线分隔的多锚点；上限 8，避免一次扩爆。"""
    raw = (keyword or "").strip()
    if not raw:
        return []
    parts = re.split(r"[,，;；|]+", raw)
    seeds = [p.strip() for p in parts if p.strip()]
    # 去重保序
    out: list[str] = []
    seen: set[str] = set()
    for s in seeds:
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= 8:
            break
    return out


def _expand_terms(keyword: str) -> list[str]:
    kw = (keyword or "").strip()
    if not kw:
        return []
    terms: set[str] = {kw, kw.lower()}
    # 拆出连续中文/英文片段，便于「周报规范」命中「周报」簇
    parts = re.findall(r"[\u4e00-\u9fff]{1,8}|[a-zA-Z0-9_]{2,20}", kw)
    for p in parts:
        terms.add(p)
        terms.add(p.lower())
    for group in _SYNONYM_GROUPS:
        hit = False
        for t in group:
            if t in kw or kw in t or any(t in p or p in t for p in parts):
                hit = True
                break
        if hit:
            terms |= set(group)
    # 保序去重，长词优先利于字面匹配
    uniq = sorted({t for t in terms if t}, key=lambda x: (-len(x), x))
    return uniq


def _expand_terms_multi(seeds: list[str]) -> list[str]:
    """多锚点各自扩展后并集；同义簇只扩一次，避免重复膨胀。"""
    terms: set[str] = set()
    for s in seeds:
        terms.update(_expand_terms(s))
    return sorted(terms, key=lambda x: (-len(x), x))


def _char_bigrams(s: str) -> set[str]:
    s = re.sub(r"\s+", "", (s or "").lower())
    if not s:
        return set()
    if len(s) == 1:
        return {s}
    return {s[i : i + 2] for i in range(len(s) - 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / len(a | b)


def _parse_msg_time(value: Any) -> float:
    """粗解析 create_time → 排序用 unix；失败返回 0。"""
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v > 10_000_000_000 else v
    s = str(value).strip().replace("T", " ")[:19]
    try:
        from datetime import datetime, timedelta, timezone

        _TZ = timezone(timedelta(hours=8))
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                raw = s if fmt != "%Y-%m-%d" else s[:10]
                dt = datetime.strptime(raw, fmt).replace(tzinfo=_TZ)
                return dt.timestamp()
            except ValueError:
                continue
    except Exception:
        pass
    return 0.0


def _score_message(
    text: str, query_terms: list[str], query_seeds: list[str]
) -> tuple[float, str]:
    """返回 (score, match_mode)。score 越高越相关。"""
    low = (text or "").lower()
    if not query_terms and not query_seeds:
        return 0.0, "none"

    seed_lows = {s.lower() for s in query_seeds if s}

    # 1) 字面 / 同义：命中词若属于用户/锚点种子 → literal，否则 synonym
    for t in query_terms:
        if t and t.lower() in low:
            mode = "literal" if t.lower() in seed_lows else "synonym"
            base = 1.0 if mode == "literal" else 0.85
            return base, mode

    # 2) 模糊：各种子 + 扩展词的 bigram Jaccard
    q_grams: set[str] = set()
    for s in query_seeds:
        q_grams |= _char_bigrams(s)
    for t in query_terms:
        q_grams |= _char_bigrams(t)
    t_grams = _char_bigrams(text)
    sim = _jaccard(q_grams, t_grams)
    if sim >= 0.18:
        return sim * 0.7, "fuzzy"
    return 0.0, "none"


def _mode_tier(mode: str) -> int:
    return {"literal": 3, "synonym": 2, "fuzzy": 1, "speaker_only": 2, "recent": 0}.get(mode, 0)


def _recency_boost(ts: float, newest_ts: float, oldest_ts: float) -> float:
    """把时间新近度折进分数（0～0.2），同相关档时明显偏向更新的消息。"""
    if ts <= 0 or newest_ts <= 0:
        return 0.0
    if newest_ts <= oldest_ts:
        return 0.1
    return 0.2 * max(0.0, min(1.0, (ts - oldest_ts) / (newest_ts - oldest_ts)))


def query_cached_chat(
    *,
    keyword: str = "",
    speaker: str = "",
    limit: int = 10,
    session_key: str | None = None,
) -> dict[str, Any]:
    """
    检索范围 = 当前会话缓存（时间窗内已 ingest 的原文；默认不按小条数帽截断）。
    排序：匹配档位（字面>同义>模糊）为主，同档 + 综合分里加重**越新越高**。
    """
    key = (session_key or "").strip() or get_active_session_key()
    kw = (keyword or "").strip()
    sp = (speaker or "").strip().lower()
    n = max(1, min(int(limit or 10), 20))

    with _lock:
        _purge_expired_unlocked()
        entry = _store.get(key)
        if not entry:
            return {
                "ok": False,
                "error": "no_cached_chat",
                "message": (
                    "No chat cache for this session (missing or TTL expired). "
                    "Call fetch_feishu_chat_digest or fetch_mock_feishu_chat_digest first."
                ),
                "hits": [],
            }
        entry["updated"] = time.time()
        messages = list(entry.get("messages") or [])
        meta = {
            "hours": entry.get("hours"),
            "range_start": entry.get("range_start"),
            "range_end": entry.get("range_end"),
            "max_messages": entry.get("max_messages"),
            "truncated": bool(entry.get("truncated")),
            "ingest_policy": entry.get("ingest_policy"),
        }

    if not messages:
        return {
            "ok": True,
            "session_key": key,
            "scope": meta,
            "hit_count": 0,
            "hits": [],
            "total_cached": 0,
        }

    # messages[0] 约定为最新
    if not kw and not sp:
        hits = messages[: min(n, 5)]
        return {
            "ok": True,
            "session_key": key,
            "scope": meta,
            "filter": {"keyword": None, "speaker": None, "note": "no filter → newest few"},
            "match_mode": "recent",
            "hit_count": len(hits),
            "hits": hits,
            "total_cached": len(messages),
        }

    seeds = _parse_keyword_seeds(kw)
    terms = _expand_terms_multi(seeds) if seeds else []
    times = [_parse_msg_time(m.get("time")) for m in messages]
    newest_ts = max(times) if times else 0.0
    oldest_ts = min((t for t in times if t > 0), default=0.0)

    scored: list[tuple[int, float, float, str, dict[str, Any]]] = []
    for m in messages:
        speaker_blob = f"{m.get('speaker') or ''} {m.get('sender_id') or ''}".lower()
        if sp and sp not in speaker_blob:
            continue
        text = m.get("text") or ""
        if seeds:
            rel, mode = _score_message(text, terms, seeds)
            if rel <= 0:
                continue
        else:
            rel, mode = 1.0, "speaker_only"
        ts = _parse_msg_time(m.get("time"))
        boost = _recency_boost(ts, newest_ts, oldest_ts)
        final = rel + boost
        scored.append((_mode_tier(mode), final, ts, mode, m))

    # 档位 → 综合分（含时效）→ 时间戳
    scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    top = scored[:n]
    hits = []
    modes: set[str] = set()
    for tier, final, _ts, mode, m in top:
        modes.add(mode)
        row = dict(m)
        row["match_score"] = round(float(final), 4)
        row["match_mode"] = mode
        row["match_tier"] = tier
        hits.append(row)

    primary_mode = "none"
    for cand in ("literal", "synonym", "fuzzy", "speaker_only"):
        if cand in modes:
            primary_mode = cand
            break

    return {
        "ok": True,
        "session_key": key,
        "scope": meta,
        "filter": {
            "keyword": keyword or None,
            "seeds": seeds or None,
            "speaker": speaker or None,
            "expanded_terms": terms[:16] if terms else [],
        },
        "match_mode": primary_mode,
        "rank_policy": "tier(literal>synonym>fuzzy) then score_with_recency_boost then newer_time",
        "hit_count": len(hits),
        "hits": hits,
        "total_cached": len(messages),
        "note": (
            "Scope = session cache. Prefer info_cards.quote_hints as keyword "
            "(comma-separated). Rank: match tier then recency. Not embedding."
        ),
    }


def load_chat_cache_index_hint() -> str:
    """短索引钥匙（塞进模型上下文）；不含原文，省 Token。"""
    idx = cache_index()
    if not idx.get("cached"):
        return ""
    names = "、".join(idx.get("speakers") or []) or "(未知)"
    exp = idx.get("expires_in_sec")
    ttl_bit = f"；约 {exp}s 后过期" if isinstance(exp, int) else ""
    rn = int(idx.get("recent_n") or 0)
    if rn > 0:
        mode_bit = f"；上次摄入 recent_n={rn}"
    else:
        hours = idx.get("hours")
        mode_bit = f"；上次摄入 hours={hours}" if hours is not None else ""
        if idx.get("range_start") and idx.get("range_end"):
            mode_bit += f"（{idx.get('range_start')} ~ {idx.get('range_end')}）"
    cap = idx.get("max_messages")
    cap_bit = f"；软上限≤{cap}条" if cap else ""
    trunc = "；窗内已截断" if idx.get("truncated") else ""
    return (
        f"[白名单会话缓存索引] 本会话已缓存 {idx.get('message_count')} 条原始消息；"
        f"参与人：{names}{mode_bit}{cap_bit}{trunc}{ttl_bit}。"
        "追问某句原话 → 优先用 info_cards.quote_hints 调 query_cached_chat；"
        "用户改 hours/recent_n、或要求重新整理/重拉 → 再调 fetch_feishu_chat_digest，"
        "勿把上一轮助手结论当成当前群聊真相。"
    )
