"""Bilibili 私人画像：Cookie（SESSDATA）拉资料 + 观看历史；可选投稿/收藏。

本机学习用；Cookie 勿提交 Git。接口为社区常用主站 API，非开放平台企业应用。
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_API = "https://api.bilibili.com"
_MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


def _load_env() -> None:
    from util.env import load_project_env

    load_project_env()


def bilibili_cookie() -> str:
    _load_env()
    return (os.getenv("BILI_COOKIE") or "").strip()


def bilibili_uid() -> Optional[int]:
    _load_env()
    raw = (os.getenv("BILI_UID") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def bilibili_configured() -> bool:
    c = bilibili_cookie()
    return "SESSDATA=" in c or (bool(c) and "SESSDATA" not in c and len(c) > 8)


def _normalize_cookie(raw: str) -> str:
    """允许只填 SESSDATA 值，或完整 Cookie 头。"""
    s = (raw or "").strip()
    if not s:
        return ""
    if "SESSDATA=" in s:
        return s
    # 用户只贴了 SESSDATA 值
    return f"SESSDATA={s}"


def _get_json(
    path: str,
    params: dict[str, Any] | None = None,
    *,
    cookie: str = "",
    timeout: float = 20.0,
) -> dict[str, Any]:
    qs = urllib.parse.urlencode(params or {}, doseq=True)
    url = f"{_API}{path}" + (f"?{qs}" if qs else "")
    headers = {
        "User-Agent": _UA,
        "Referer": "https://www.bilibili.com",
        "Accept": "application/json, text/plain, */*",
    }
    ck = _normalize_cookie(cookie)
    if ck:
        headers["Cookie"] = ck
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"Bilibili HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Bilibili network error: {e.reason}") from e
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Bilibili response not JSON: {e}") from e
    return data


def _require_ok(payload: dict[str, Any], *, what: str) -> Any:
    code = payload.get("code")
    if code == -101:
        raise RuntimeError(
            f"{what}: 未登录或 Cookie 失效 (code=-101)。请重新从浏览器复制含 SESSDATA 的 Cookie。"
        )
    if code != 0:
        raise RuntimeError(f"{what}: code={code} message={payload.get('message')}")
    return payload.get("data")


def _mixin_key(img_key: str, sub_key: str) -> str:
    raw = img_key + sub_key
    return functools.reduce(lambda s, i: s + raw[i], _MIXIN_TAB, "")[:32]


def _wbi_keys(cookie: str = "") -> tuple[str, str]:
    nav = _get_json("/x/web-interface/nav", cookie=cookie)
    wbi = ((nav.get("data") or {}).get("wbi_img")) or {}
    img = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
    sub = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
    if not img or not sub:
        raise RuntimeError("无法获取 WBI 签名密钥（nav.wbi_img）")
    return img, sub


def _sign_wbi(params: dict[str, Any], cookie: str = "") -> dict[str, Any]:
    img_key, sub_key = _wbi_keys(cookie)
    mixin = _mixin_key(img_key, sub_key)
    out = {k: v for k, v in params.items() if v is not None}
    out["wts"] = int(time.time())
    out = dict(sorted(out.items()))
    filtered = {
        k: "".join(ch for ch in str(v) if ch not in "!'()*") for k, v in out.items()
    }
    query = urllib.parse.urlencode(filtered)
    filtered["w_rid"] = hashlib.md5((query + mixin).encode("utf-8")).hexdigest()
    return filtered


def fetch_nav_profile(cookie: str) -> dict[str, Any]:
    data = _require_ok(_get_json("/x/web-interface/nav", cookie=cookie), what="nav")
    if not isinstance(data, dict) or not data.get("isLogin"):
        raise RuntimeError("Cookie 无效：nav.isLogin 为 false")
    level_info = data.get("level_info") or {}
    return {
        "mid": data.get("mid"),
        "name": data.get("uname"),
        "face": data.get("face"),
        "level": level_info.get("current_level"),
        "vip_due": (data.get("vipDueDate") or 0),
        "vip_status": data.get("vipStatus"),
        "money": data.get("money"),
        "moral": data.get("moral"),
        "official": (data.get("official_verify") or {}).get("desc")
        or (data.get("official") or {}).get("title"),
    }


def fetch_relation_stat(mid: int, cookie: str = "") -> dict[str, Any]:
    data = _require_ok(
        _get_json("/x/relation/stat", {"vmid": mid}, cookie=cookie),
        what="relation/stat",
    )
    return {
        "following": (data or {}).get("following"),
        "follower": (data or {}).get("follower"),
        "whisper": (data or {}).get("whisper"),
        "black": (data or {}).get("black"),
    }


def fetch_card(mid: int, cookie: str = "") -> dict[str, Any]:
    data = _require_ok(
        _get_json("/x/web-interface/card", {"mid": mid}, cookie=cookie),
        what="web-interface/card",
    ) or {}
    card = data.get("card") or {}
    return {
        "mid": card.get("mid") or mid,
        "name": card.get("name"),
        "sign": card.get("sign"),
        "level": (card.get("level_info") or {}).get("current_level"),
        "sex": card.get("sex"),
        "archive_count": data.get("archive_count"),
        "article_count": data.get("article_count"),
        "follower": data.get("follower"),
        "following": data.get("following"),
        "like_num": data.get("like_num"),
    }


def fetch_watch_history(
    cookie: str,
    *,
    max_items: int = 40,
    hist_type: str = "archive",
) -> list[dict[str, Any]]:
    """观看历史（需 SESSDATA）。默认只要稿件视频。"""
    n = max(5, min(int(max_items or 40), 300))
    out: list[dict[str, Any]] = []
    cursor: dict[str, Any] = {"ps": min(30, n), "type": hist_type}
    while len(out) < n:
        data = _require_ok(
            _get_json("/x/web-interface/history/cursor", cursor, cookie=cookie),
            what="history/cursor",
        ) or {}
        batch = data.get("list") or []
        if not batch:
            break
        for item in batch:
            if len(out) >= n:
                break
            hist = item.get("history") or {}
            out.append(
                {
                    "title": item.get("title"),
                    "bvid": hist.get("bvid"),
                    "aid": hist.get("oid"),
                    "cid": hist.get("cid"),
                    "business": hist.get("business") or item.get("business"),
                    "author_name": item.get("author_name"),
                    "author_mid": item.get("author_mid"),
                    "cover": item.get("cover"),
                    "progress": item.get("progress"),
                    "duration": item.get("duration"),
                    "view_at": item.get("view_at"),
                    "tag_name": item.get("tag_name"),
                    "uri": item.get("uri"),
                }
            )
        cur = data.get("cursor") or {}
        if not cur.get("max") or not cur.get("view_at"):
            break
        cursor = {
            "ps": min(30, n - len(out)),
            "type": hist_type,
            "max": cur.get("max"),
            "view_at": cur.get("view_at"),
            "business": cur.get("business") or "",
        }
        if cursor["ps"] <= 0:
            break
    return out


def fetch_uploads(mid: int, cookie: str = "", *, max_items: int = 20) -> list[dict[str, Any]]:
    """空间投稿（WBI）。"""
    n = max(1, min(int(max_items or 20), 50))
    params = _sign_wbi(
        {"mid": mid, "pn": 1, "ps": n, "order": "pubdate"},
        cookie=cookie,
    )
    payload = _get_json("/x/space/wbi/arc/search", params, cookie=cookie)
    data = _require_ok(payload, what="space/wbi/arc/search") or {}
    vlist = ((data.get("list") or {}).get("vlist")) or []
    videos = []
    for v in vlist:
        videos.append(
            {
                "title": v.get("title"),
                "bvid": v.get("bvid"),
                "aid": v.get("aid"),
                "play": v.get("play"),
                "comment": v.get("comment"),
                "created": v.get("created"),
                "length": v.get("length"),
                "description": (v.get("description") or "")[:160],
            }
        )
    return videos


def fetch_fav_folders(mid: int, cookie: str = "") -> list[dict[str, Any]]:
    payload = _get_json(
        "/x/v3/fav/folder/created/list-all",
        {"up_mid": mid},
        cookie=cookie,
    )
    # 无公开收藏时 data 可能为 null
    if payload.get("code") != 0:
        raise RuntimeError(
            f"fav/folder: code={payload.get('code')} message={payload.get('message')}"
        )
    data = payload.get("data") or {}
    folders = data.get("list") or []
    return [
        {
            "id": f.get("id"),
            "title": f.get("title"),
            "media_count": f.get("media_count"),
            "fav_state": f.get("fav_state"),
            "attr": f.get("attr"),
        }
        for f in folders
    ]


def fetch_followings(
    mid: int,
    cookie: str = "",
    *,
    max_items: int = 30,
) -> list[dict[str, Any]]:
    """关注列表（需登录 Cookie；隐私设置可能导致部分不可见）。"""
    n = max(5, min(int(max_items or 30), 50))
    out: list[dict[str, Any]] = []
    pn = 1
    ps = min(50, n)
    while len(out) < n:
        data = _require_ok(
            _get_json(
                "/x/relation/followings",
                {"vmid": mid, "pn": pn, "ps": ps, "order_type": "attention"},
                cookie=cookie,
            ),
            what="relation/followings",
        ) or {}
        batch = data.get("list") or []
        if not batch:
            break
        for u in batch:
            if len(out) >= n:
                break
            out.append(
                {
                    "mid": u.get("mid"),
                    "name": u.get("uname"),
                    "sign": (u.get("sign") or "")[:80],
                    "face": None,  # 去掉头像省 token；完整在 raw
                    "official": ((u.get("official_verify") or {}).get("desc"))
                    or ((u.get("official") or {}).get("title")),
                    "attribute": u.get("attribute"),
                    "mtime": u.get("mtime"),
                }
            )
        total = int((data.get("total") or 0))
        if pn * ps >= total or len(batch) < ps:
            break
        pn += 1
    return out


def lookup_up_bundle(mid: int, cookie: str = "", *, uploads_n: int = 10) -> dict[str, Any]:
    """按 mid 查看 UP 空间卡片 + 近期投稿（按需调用，勿默认连环展开）。"""
    mid_i = int(mid)
    card = fetch_card(mid_i, cookie)
    try:
        stat = fetch_relation_stat(mid_i, cookie)
        card.update({k: stat.get(k) for k in ("following", "follower") if stat.get(k) is not None})
    except Exception as e:
        card["relation_error"] = str(e)
    uploads: list[dict[str, Any]] = []
    uploads_error = None
    try:
        if uploads_n and uploads_n > 0:
            uploads = fetch_uploads(mid_i, cookie, max_items=uploads_n)
    except Exception as e:
        uploads_error = str(e)
    out: dict[str, Any] = {
        "source": "bilibili_up",
        "mid": mid_i,
        "space_url": f"https://space.bilibili.com/{mid_i}",
        "profile": card,
        "uploads": uploads,
    }
    if uploads_error:
        out["uploads_error"] = uploads_error
    return out


def slim_bilibili_for_agent(bundle: dict[str, Any]) -> dict[str, Any]:
    """给模型的精简包（去掉封面 URL 等噪声）。"""
    profile = dict(bundle.get("profile") or {})
    profile.pop("face", None)
    hist = []
    for h in bundle.get("history") or []:
        hist.append(
            {
                "title": h.get("title"),
                "bvid": h.get("bvid"),
                "author_name": h.get("author_name"),
                "author_mid": h.get("author_mid"),
                "business": h.get("business"),
                "progress": h.get("progress"),
                "duration": h.get("duration"),
                "view_at": h.get("view_at"),
                "tag_name": h.get("tag_name"),
            }
        )
    uploads = []
    for v in bundle.get("uploads") or []:
        uploads.append(
            {
                "title": v.get("title"),
                "bvid": v.get("bvid"),
                "play": v.get("play"),
                "created": v.get("created"),
                "length": v.get("length"),
            }
        )
    followings = []
    for u in bundle.get("followings") or []:
        followings.append(
            {
                "mid": u.get("mid"),
                "name": u.get("name"),
                "sign": u.get("sign"),
                "official": u.get("official"),
            }
        )
    slim: dict[str, Any] = {
        "source": bundle.get("source") or "bilibili_private",
        "profile": profile,
        "history_count": len(hist),
        "history": hist,
        "uploads_count": len(uploads),
        "uploads": uploads,
        "favorites": bundle.get("favorites") or [],
        "usage_note": (
            "私人 B 站轨迹/元数据；勿朗读 JSON。"
            "仅含标题与短字段，不含视频正文或完整剧情。"
            "用户问「讲了什么/剧情/内容」而本包不够时：必须再调 web_search，禁止用标题脑补完结。"
            "关注列表/UP 深挖仍须用户要细说或同意（内容缺口升级 web 除外）。"
        ),
    }
    if followings:
        slim["followings_count"] = len(followings)
        slim["followings"] = followings
    if bundle.get("space_url"):
        slim["space_url"] = bundle.get("space_url")
        slim["mid"] = bundle.get("mid")
    return slim


def fetch_bilibili_private_bundle(
    *,
    history_n: int = 30,
    uploads_n: int = 0,
    include_favorites: bool = False,
) -> dict[str, Any]:
    """默认只要资料+观看历史；投稿/收藏按需再拉，避免无谓耗时与 token。"""
    cookie = bilibili_cookie()
    if not bilibili_configured():
        raise RuntimeError(
            "Bilibili not configured. Set BILI_COOKIE (须含 SESSDATA) in project root .env"
        )

    profile = fetch_nav_profile(cookie)
    mid = int(profile.get("mid") or 0)
    env_mid = bilibili_uid()
    if env_mid and mid and env_mid != mid:
        profile["env_uid_mismatch"] = env_mid
    if not mid and env_mid:
        mid = env_mid
        profile["mid"] = mid

    try:
        stat = fetch_relation_stat(mid, cookie)
        profile.update(stat)
    except Exception as e:
        profile["relation_error"] = str(e)

    try:
        card = fetch_card(mid, cookie)
        for k in ("sign", "archive_count", "article_count", "like_num", "sex"):
            if card.get(k) is not None:
                profile[k] = card.get(k)
        if profile.get("follower") is None:
            profile["follower"] = card.get("follower")
        if profile.get("following") is None:
            profile["following"] = card.get("following")
        if profile.get("level") is None:
            profile["level"] = card.get("level")
    except Exception as e:
        profile["card_error"] = str(e)

    history = fetch_watch_history(cookie, max_items=history_n)

    uploads: list[dict[str, Any]] = []
    uploads_error = None
    if uploads_n and uploads_n > 0:
        try:
            uploads = fetch_uploads(mid, cookie, max_items=uploads_n)
        except Exception as e:
            uploads_error = str(e)

    favorites: list[dict[str, Any]] = []
    fav_error = None
    if include_favorites:
        try:
            favorites = fetch_fav_folders(mid, cookie)
        except Exception as e:
            fav_error = str(e)

    bundle: dict[str, Any] = {
        "source": "bilibili_private",
        "endpoint": "nav+history/cursor",
        "profile": profile,
        "history": history,
        "uploads": uploads,
        "favorites": favorites,
    }
    if uploads_error:
        bundle["uploads_error"] = uploads_error
    if fav_error:
        bundle["favorites_error"] = fav_error
    return bundle


def fetch_bilibili_followings_bundle(*, max_items: int = 30) -> dict[str, Any]:
    cookie = bilibili_cookie()
    if not bilibili_configured():
        raise RuntimeError("Bilibili not configured. Set BILI_COOKIE in .env")
    profile = fetch_nav_profile(cookie)
    mid = int(profile.get("mid") or bilibili_uid() or 0)
    if not mid:
        raise RuntimeError("无法解析 mid：检查 Cookie 或设置 BILI_UID")
    followings = fetch_followings(mid, cookie, max_items=max_items)
    return {
        "source": "bilibili_followings",
        "profile": {"mid": mid, "name": profile.get("name")},
        "followings": followings,
        "history": [],
        "uploads": [],
        "favorites": [],
    }
