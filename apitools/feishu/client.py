"""飞书开放平台：tenant token、日历读写、多维表格计划、白名单会话摘要。"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

FEISHU_API = "https://open.feishu.cn/open-apis"
_UA = "cyber_lobster_api_fusion/0.5 (+feishu)"
_TZ_CN = timezone(timedelta(hours=8))

_token_cache: dict[str, Any] = {"token": None, "expire_at": 0.0}
_calendar_id_cache: dict[str, str] = {}
_calendar_id_lock = threading.Lock()


def _load_env() -> None:
    from util.env import load_project_env

    load_project_env()


def feishu_credentials() -> tuple[Optional[str], Optional[str]]:
    _load_env()
    app_id = (os.getenv("FEISHU_APP_ID") or "").strip()
    app_secret = (os.getenv("FEISHU_APP_SECRET") or "").strip()
    return (app_id or None, app_secret or None)


def feishu_configured() -> bool:
    app_id, app_secret = feishu_credentials()
    return bool(app_id and app_secret)


def feishu_bitable_ids() -> tuple[Optional[str], Optional[str]]:
    """优先 .env，其次 workspace 本地缓存。不保证已创建，仅读取已有配置。"""
    _load_env()
    app_token = (os.getenv("FEISHU_BITABLE_APP_TOKEN") or "").strip()
    table_id = (os.getenv("FEISHU_BITABLE_TABLE_ID") or "").strip()
    if app_token and table_id:
        return app_token, table_id
    cached = _load_bitable_cache()
    if cached:
        return cached.get("app_token"), cached.get("table_id")
    return None, None


def feishu_bitable_configured() -> bool:
    """只要飞书应用凭证齐全即可用计划表（缺 ID 时会自动建表并缓存）。"""
    return feishu_configured()


def _workspace_dir() -> Path:
    # apitools/feishu/client.py -> cyber_lobster_api_fusion/
    return Path(__file__).resolve().parents[2] / "workspace"


def _bitable_cache_path() -> Path:
    return _workspace_dir() / "feishu_bitable.json"


def _load_bitable_cache() -> Optional[dict[str, str]]:
    path = _bitable_cache_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    app_token = str(data.get("app_token") or "").strip()
    table_id = str(data.get("table_id") or "").strip()
    if app_token and table_id:
        return {"app_token": app_token, "table_id": table_id}
    return None


def _save_bitable_cache(app_token: str, table_id: str, *, url_hint: str = "") -> None:
    path = _bitable_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "app_token": app_token,
        "table_id": table_id,
        "table_url_hint": url_hint or f"https://feishu.cn/base/{app_token}",
        "updated_at": datetime.now(tz=_TZ_CN).strftime("%Y-%m-%d %H:%M:%S"),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_plan_table() -> tuple[str, str]:
    """
    解析近日计划表 ID：
    1) .env 的 FEISHU_BITABLE_*（手动指定）
    2) workspace/feishu_bitable.json（自动创建后的缓存）
    3) 调飞书 API 新建多维表 +「近日计划」数据表，并写入缓存
    """
    app_token, table_id = feishu_bitable_ids()
    if app_token and table_id:
        return str(app_token), str(table_id)

    if not feishu_configured():
        raise RuntimeError(
            "Feishu not configured. Set FEISHU_APP_ID and FEISHU_APP_SECRET in project root .env"
        )

    tok = _auth_token()
    # 1) 创建多维表格文档
    created = _http_json(
        "POST",
        "/bitable/v1/apps",
        body={"name": "阿拉蕾近日计划"},
        token=tok,
    )
    app = (created.get("data") or {}).get("app") or {}
    app_token = str(app.get("app_token") or "").strip()
    if not app_token:
        raise RuntimeError(f"Create bitable failed: missing app_token in {created}")

    default_table = str(app.get("default_table_id") or "").strip()
    table_id = ""

    # 2) 新建带约定字段的数据表
    table_body = {
        "table": {
            "name": "近日计划",
            "default_view_name": "默认视图",
            "fields": [
                {"field_name": "事项", "type": 1},
                {"field_name": "日期", "type": 1},
                {"field_name": "优先级", "type": 1},
                {"field_name": "来源", "type": 1},
                {"field_name": "状态", "type": 1},
                {"field_name": "备注", "type": 1},
            ],
        }
    }
    try:
        table_resp = _http_json(
            "POST",
            f"/bitable/v1/apps/{urllib.parse.quote(app_token)}/tables",
            body=table_body,
            token=tok,
        )
        table_id = str(
            ((table_resp.get("data") or {}).get("table_id"))
            or ((table_resp.get("data") or {}).get("table") or {}).get("table_id")
            or ""
        ).strip()
    except Exception:
        table_id = ""

    if not table_id:
        table_id = default_table
    if not table_id:
        # 3) 列出已有表兜底
        listed = _http_json(
            "GET",
            f"/bitable/v1/apps/{urllib.parse.quote(app_token)}/tables",
            params={"page_size": 20},
            token=tok,
        )
        items = ((listed.get("data") or {}).get("items")) or []
        if items:
            table_id = str(items[0].get("table_id") or "").strip()

    if not table_id:
        raise RuntimeError(
            "Created bitable but could not resolve table_id. "
            "Open the new base in Feishu and set FEISHU_BITABLE_* in .env once."
        )

    url_hint = f"https://feishu.cn/base/{app_token}"
    _save_bitable_cache(app_token, table_id, url_hint=url_hint)
    return app_token, table_id


def feishu_calendar_id_override() -> Optional[str]:
    _load_env()
    cid = (os.getenv("FEISHU_CALENDAR_ID") or "").strip()
    return cid or None


def feishu_plan_chat_ids() -> list[str]:
    _load_env()
    raw = (os.getenv("FEISHU_PLAN_CHAT_IDS") or "").strip()
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


def feishu_chat_configured() -> bool:
    return feishu_configured() and bool(feishu_plan_chat_ids())


def feishu_chat_auth_mode() -> str:
    """白名单会话拉消息身份：bot（默认，应用/tenant）| user（OAuth 用户身份）。"""
    _load_env()
    mode = (os.getenv("FEISHU_CHAT_AUTH_MODE") or "bot").strip().lower()
    if mode in ("user", "user_access", "oauth"):
        return "user"
    return "bot"


def _chat_access_token() -> str:
    """按 FEISHU_CHAT_AUTH_MODE 取 token；user 模式强制用户身份，不做 tenant 静默回退。"""
    if feishu_chat_auth_mode() == "user":
        return _require_user_access_token()
    return get_tenant_access_token()


# 常见飞书错误码 → 可操作中文提示（给 Agent / 冒烟脚本 / 日志）
_FEISHU_ERROR_HINTS: dict[int, str] = {
    190007: (
        "应用机器人身份写个人主日历失败。请确认：①已跑 feishu_oauth_login.py；"
        "②未用 tenant/app token 写个人日历；③勿并行连调多条 create（用 create_feishu_events）。"
    ),
    99991679: (
        "当前 user_access_token 缺少所需用户身份权限。"
        "请到开放平台开通对应「用户身份权限」后重新跑 feishu_oauth_login.py（旧 token 不会自动带新 scope）。"
    ),
    99991663: (
        "token 无效或已过期。请重跑 feishu_oauth_login.py，或检查 refresh_token 是否仍有效。"
    ),
    99991664: (
        "token 已过期。请重跑 feishu_oauth_login.py。"
    ),
    99991400: (
        "无权限调用该接口。检查应用是否开通对应权限，以及是否用对了 user / tenant token。"
    ),
    1254045: (
        "多维表格或数据表不存在。删除 workspace/feishu_bitable.json 后重试自动建表，"
        "或检查 FEISHU_BITABLE_APP_TOKEN / TABLE_ID。"
    ),
}


def _hint_for_feishu_code(code: Any) -> str:
    try:
        c = int(code)
    except (TypeError, ValueError):
        return ""
    return _FEISHU_ERROR_HINTS.get(c, "")


def _format_feishu_api_error(code: Any, msg: Any, *, extra: str = "") -> str:
    base = f"Feishu API error code={code} msg={msg}"
    if extra:
        base = f"{base} {extra}".rstrip()
    hint = _hint_for_feishu_code(code)
    if hint:
        return f"{base}\n→ {hint}"
    return base


_health_cache: dict[str, Any] = {"sig": None, "at": 0.0, "body": None}
_HEALTH_CACHE_TTL_SEC = 5.0


def feishu_auth_health() -> dict[str, Any]:
    """只读健康检查：应用凭证、OAuth 缓存、关键 scope、计划表缓存。不调写接口。"""
    from apitools.feishu.oauth import (
        DEFAULT_SCOPES,
        load_token_bundle,
        token_cache_path,
    )

    _load_env()
    token_path_obj = token_cache_path()
    try:
        tok_mtime = token_path_obj.stat().st_mtime if token_path_obj.is_file() else 0.0
    except OSError:
        tok_mtime = 0.0
    bitable_path = _bitable_cache_path()
    try:
        bit_mtime = bitable_path.stat().st_mtime if bitable_path.is_file() else 0.0
    except OSError:
        bit_mtime = 0.0
    sig = (
        tok_mtime,
        bit_mtime,
        (os.getenv("FEISHU_APP_ID") or "").strip(),
        (os.getenv("FEISHU_PLAN_CHAT_IDS") or "").strip(),
        (os.getenv("FEISHU_CHAT_AUTH_MODE") or "bot").strip().lower(),
    )
    now = time.time()
    cached = _health_cache.get("body")
    if (
        cached is not None
        and _health_cache.get("sig") == sig
        and (now - float(_health_cache.get("at") or 0)) < _HEALTH_CACHE_TTL_SEC
    ):
        return dict(cached)

    app_id, app_secret = feishu_credentials()
    app_ok = bool(app_id and app_secret)
    issues: list[str] = []
    if not app_ok:
        issues.append("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET")

    bundle = load_token_bundle()
    token_path = str(token_path_obj)
    has_bundle = bool(bundle)
    scope_raw = ""
    expires_at = None
    refresh_ok = False
    env_tok = (os.getenv("FEISHU_USER_ACCESS_TOKEN") or "").strip()
    if bundle:
        scope_raw = str(bundle.get("scope") or "").strip()
        expires_at = bundle.get("expires_at")
        refresh_ok = bool(str(bundle.get("refresh_token") or "").strip())
        if not refresh_ok:
            issues.append("OAuth 缓存无 refresh_token，过期后需重跑 feishu_oauth_login.py")
    elif not env_tok and app_ok:
        issues.append(
            f"无用户 token：请跑 feishu_oauth_login.py（期望缓存 {token_path}）"
        )
    elif env_tok and not has_bundle:
        issues.append(
            "仅有 FEISHU_USER_ACCESS_TOKEN（无 refresh）；建议改用 feishu_oauth_login.py"
        )

    scope_tokens = set(scope_raw.split()) if scope_raw else set()
    need_groups = {
        "calendar_write": ("calendar:calendar.event:create", "calendar:calendar"),
        "bitable": ("bitable:app",),
        "bitable_create": ("base:app:create",),
        "offline": ("offline_access",),
    }
    scope_ok: dict[str, bool] = {}
    for name, alts in need_groups.items():
        ok_g = any(s in scope_tokens for s in alts) if scope_tokens else False
        scope_ok[name] = ok_g
        if has_bundle and not ok_g:
            issues.append(
                f"OAuth scope 缺少 {name}（需要其一: {', '.join(alts)}）→ 改权限后重跑 oauth"
            )

    bitable_app, bitable_table = feishu_bitable_ids()
    plan_cached = bool(bitable_app and bitable_table)

    write_ready = app_ok and (
        (has_bundle and scope_ok.get("calendar_write", False))
        or (bool(env_tok) and not has_bundle)
    )
    # 有 OAuth 缓存时必须以 bitable scope 为准；仅 env token 时无法验 scope，标为未知可试
    if has_bundle:
        bitable_ready = app_ok and scope_ok.get("bitable", False)
    else:
        bitable_ready = app_ok  # 运行时再验

    ok = write_ready and bitable_ready

    body = {
        "ok": bool(ok),
        "app_configured": app_ok,
        "user_token_cached": has_bundle,
        "token_path": token_path,
        "has_refresh_token": refresh_ok,
        "expires_at": expires_at,
        "scope": scope_raw or None,
        "scope_checks": scope_ok,
        "expected_scopes": DEFAULT_SCOPES.strip(),
        "plan_table_cached": plan_cached,
        "plan_app_token": bitable_app,
        "plan_table_id": bitable_table,
        "chat_whitelist": feishu_chat_configured(),
        "chat_auth_mode": feishu_chat_auth_mode(),
        "write_calendar_ready": write_ready,
        "bitable_ready": bitable_ready,
        "issues": issues,
    }
    _health_cache["sig"] = sig
    _health_cache["at"] = now
    _health_cache["body"] = body
    return dict(body)


def _http_json(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: float = 25.0,
) -> dict[str, Any]:
    url = f"{FEISHU_API}{path}"
    if params:
        qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{url}?{qs}"
    headers = {
        "User-Agent": _UA,
        "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:800]
        try:
            err_payload = json.loads(detail)
            err_code = err_payload.get("code")
            err_msg = err_payload.get("msg") or err_payload
            raise RuntimeError(
                _format_feishu_api_error(err_code, err_msg, extra=f"(HTTP {e.code})")
            ) from e
        except (json.JSONDecodeError, TypeError):
            raise RuntimeError(f"Feishu HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Feishu network error: {e.reason}") from e
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Feishu response is not JSON: {e}") from e
    code = payload.get("code")
    if code not in (0, None):
        raise RuntimeError(
            _format_feishu_api_error(code, payload.get("msg") or payload)
        )
    return payload


def get_tenant_access_token(*, force_refresh: bool = False) -> str:
    now = time.time()
    cached = _token_cache.get("token")
    expire_at = float(_token_cache.get("expire_at") or 0)
    if not force_refresh and cached and now < expire_at - 60:
        return str(cached)
    app_id, app_secret = feishu_credentials()
    if not app_id or not app_secret:
        raise RuntimeError(
            "Feishu not configured. Set FEISHU_APP_ID and FEISHU_APP_SECRET in project root .env"
        )
    payload = _http_json(
        "POST",
        "/auth/v3/tenant_access_token/internal",
        body={"app_id": app_id, "app_secret": app_secret},
    )
    token = payload.get("tenant_access_token")
    if not token:
        raise RuntimeError(f"Feishu token missing in response: {payload}")
    expire = int(payload.get("expire") or 7200)
    _token_cache["token"] = token
    _token_cache["expire_at"] = now + expire
    return str(token)


def _user_access_token() -> Optional[str]:
    """优先 OAuth 本地缓存（可 refresh），其次 .env FEISHU_USER_ACCESS_TOKEN。

    注意：若本地已有 oauth 缓存但 refresh 失败，必须抛错，禁止静默退回 tenant
   （否则会出现 190007 app bot_id not found）。
    """
    from apitools.feishu.oauth import get_valid_user_access_token, load_token_bundle

    bundle = load_token_bundle()
    if bundle:
        return get_valid_user_access_token()
    _load_env()
    tok = (os.getenv("FEISHU_USER_ACCESS_TOKEN") or "").strip()
    return tok or None


def _require_user_access_token() -> str:
    tok = _user_access_token()
    if tok:
        return tok
    raise RuntimeError(
        "User calendar write needs user_access_token. "
        "Run: python feishu_oauth_login.py "
        "(do not fall back to app/tenant token — that causes 190007 app bot_id not found)."
    )


def _auth_token() -> str:
    """优先用户 token；否则 tenant token（仅适合读/建表等；写个人日历请用 _require_user_access_token）。"""
    user = _user_access_token()
    if user:
        return user
    return get_tenant_access_token()


def resolve_calendar_id(token: str | None = None) -> str:
    override = feishu_calendar_id_override()
    if override:
        return override
    tok = token or _auth_token()
    cache_key = "primary"
    with _calendar_id_lock:
        cached = _calendar_id_cache.get(cache_key)
        if cached:
            return cached
    payload = _http_json("POST", "/calendar/v4/calendars/primary", body={}, token=tok)
    data = payload.get("data") or {}
    cals = data.get("calendars") or data.get("calendar_list") or []
    cid = None
    if isinstance(cals, list) and cals:
        first = cals[0]
        if isinstance(first, dict):
            cal = first.get("calendar") or first
            cid = (cal.get("calendar_id") if isinstance(cal, dict) else None) or first.get(
                "calendar_id"
            )
    if not cid:
        cal = data.get("calendar") or {}
        cid = cal.get("calendar_id")
    if not cid:
        raise RuntimeError(
            "Cannot resolve primary calendar_id. Re-run feishu_oauth_login.py "
            "or set FEISHU_CALENDAR_ID in .env."
        )
    cid_s = str(cid)
    with _calendar_id_lock:
        _calendar_id_cache[cache_key] = cid_s
    return cid_s


def _parse_dt_to_unix(value: str | int | float | None, *, default: datetime | None = None) -> int:
    if value is None or value == "":
        if default is None:
            raise ValueError("datetime required")
        return int(default.timestamp())
    if isinstance(value, (int, float)):
        v = int(value)
        return v // 1000 if v > 10_000_000_000 else v
    s = str(value).strip()
    # 归一化：全角数字旁空白、en/em dash、T
    s = (
        s.replace("–", "-")
        .replace("—", "-")
        .replace("/", "-")
        .replace("年", "-")
        .replace("月", "-")
        .replace("日", " ")
        .replace("．", ".")
    )
    s = " ".join(s.split())
    if s.isdigit():
        v = int(s)
        return v // 1000 if v > 10_000_000_000 else v
    # 容错 2026-009-18 → 2026-09-18
    parts = s.replace("T", " ").split(" ")
    if parts:
        date_bits = parts[0].split("-")
        if len(date_bits) == 3:
            y, m, d = date_bits
            if y.isdigit() and m.isdigit() and d.isdigit():
                parts[0] = f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
                s = " ".join(parts)
    for fmt in (
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            raw = s[:19] if ("T" in s or " " in s) and len(s) >= 16 else s
            if fmt == "%Y-%m-%d":
                raw = s[:10]
            dt = datetime.strptime(raw, fmt)
            if fmt == "%Y-%m-%d":
                dt = dt.replace(hour=0, minute=0, second=0)
            return int(dt.replace(tzinfo=_TZ_CN).timestamp())
        except ValueError:
            continue
    raise ValueError(f"Unrecognized datetime: {value!r} (use 'YYYY-MM-DD HH:MM')")


def _fmt_ts(ts: Any) -> str:
    try:
        v = int(ts)
        if v > 10_000_000_000:
            v //= 1000
        return datetime.fromtimestamp(v, tz=_TZ_CN).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts or "")


def slim_calendar_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for ev in events:
        start = ev.get("start_time") or {}
        end = ev.get("end_time") or {}
        out.append(
            {
                "event_id": ev.get("event_id"),
                "summary": ev.get("summary") or "(无标题)",
                "description": (ev.get("description") or "")[:200],
                "start": _fmt_ts(start.get("timestamp") or start.get("date")),
                "end": _fmt_ts(end.get("timestamp") or end.get("date")),
                "status": ev.get("status"),
            }
        )
    return out


def fetch_calendar_events(*, days: int = 3, calendar_id: str | None = None) -> dict[str, Any]:
    """拉取未来 days 天（含今天）的日程列表。"""
    tok = _auth_token()
    cid = calendar_id or resolve_calendar_id(tok)
    n = max(1, min(int(days or 3), 14))
    now = datetime.now(tz=_TZ_CN)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=n)
    params = {
        "start_time": str(int(start.timestamp())),
        "end_time": str(int(end.timestamp())),
        "page_size": 50,
    }
    items: list[dict[str, Any]] = []
    page_token = None
    for _ in range(5):
        p = dict(params)
        if page_token:
            p["page_token"] = page_token
        payload = _http_json(
            "GET",
            f"/calendar/v4/calendars/{urllib.parse.quote(cid)}/events",
            params=p,
            token=tok,
        )
        data = payload.get("data") or {}
        batch = data.get("items") or []
        items.extend(batch)
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
        if not page_token:
            break
    slim = slim_calendar_events(items)
    return {
        "source": "feishu_calendar",
        "calendar_id": cid,
        "days": n,
        "range_start": start.strftime("%Y-%m-%d %H:%M"),
        "range_end": end.strftime("%Y-%m-%d %H:%M"),
        "event_count": len(slim),
        "events": slim,
        "auth_mode": "user" if _user_access_token() else "tenant",
    }


def create_calendar_event(
    *,
    summary: str,
    start_at: str,
    end_at: str | None = None,
    description: str = "",
    calendar_id: str | None = None,
) -> dict[str, Any]:
    """创建日程。start_at/end_at 格式 YYYY-MM-DD HH:MM；未给 end 则默认 +1 小时。

    写个人主日历强制 user_access_token，避免并行刷新失败后误用 tenant → 190007。
    """
    title = (summary or "").strip()
    if not title:
        raise ValueError("summary is required")
    start_unix = _parse_dt_to_unix(start_at)
    if end_at:
        end_unix = _parse_dt_to_unix(end_at)
    else:
        end_unix = start_unix + 3600
    if end_unix <= start_unix:
        raise ValueError("end_at must be after start_at")
    tok = _require_user_access_token()
    cid = calendar_id or resolve_calendar_id(tok)
    body = {
        "summary": title,
        "description": (description or "")[:4000],
        "start_time": {"timestamp": str(start_unix), "timezone": "Asia/Shanghai"},
        "end_time": {"timestamp": str(end_unix), "timezone": "Asia/Shanghai"},
    }
    payload = _http_json(
        "POST",
        f"/calendar/v4/calendars/{urllib.parse.quote(cid)}/events",
        body=body,
        token=tok,
    )
    data = payload.get("data") or {}
    event = data.get("event") or data
    return {
        "source": "feishu_calendar_create",
        "calendar_id": cid,
        "event_id": event.get("event_id"),
        "summary": event.get("summary") or title,
        "start": _fmt_ts(start_unix),
        "end": _fmt_ts(end_unix),
        "auth_mode": "user",
    }


def create_calendar_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """串行创建多条日程，避免并行 refresh 竞态。每项含 summary/start_at，可选 end_at/description。"""
    if not events:
        raise ValueError("events is empty")
    # 先拿一次 user token，把 refresh 做完
    _require_user_access_token()
    cid = resolve_calendar_id()
    created: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for i, raw in enumerate(events[:20]):
        if not isinstance(raw, dict):
            failed.append({"index": i, "error": "not an object"})
            continue
        try:
            item = create_calendar_event(
                summary=str(raw.get("summary") or raw.get("标题") or ""),
                start_at=str(raw.get("start_at") or raw.get("开始") or ""),
                end_at=(str(raw.get("end_at") or raw.get("结束") or "").strip() or None),
                description=str(raw.get("description") or raw.get("备注") or ""),
                calendar_id=cid,
            )
            created.append(item)
            time.sleep(0.15)
        except Exception as e:
            failed.append(
                {
                    "index": i,
                    "summary": raw.get("summary") or raw.get("标题"),
                    "start_at": raw.get("start_at") or raw.get("开始"),
                    "error": str(e)[:300],
                }
            )
    return {
        "source": "feishu_calendar_create_batch",
        "calendar_id": cid,
        "created_count": len(created),
        "failed_count": len(failed),
        "created": created,
        "failed": failed,
        "auth_mode": "user",
    }


def _row_fields(record: dict[str, Any]) -> dict[str, Any]:
    fields = record.get("fields") or {}
    # 文本字段有时是 [{text, type}] 列表
    out: dict[str, Any] = {}
    for k, v in fields.items():
        if isinstance(v, list) and v and isinstance(v[0], dict) and "text" in v[0]:
            out[k] = "".join(str(x.get("text") or "") for x in v)
        else:
            out[k] = v
    return out


def slim_plan_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in records:
        fields = _row_fields(r)
        out.append(
            {
                "record_id": r.get("record_id"),
                "日期": fields.get("日期") or fields.get("date"),
                "事项": fields.get("事项") or fields.get("title") or fields.get("事项名称"),
                "优先级": fields.get("优先级") or fields.get("priority"),
                "来源": fields.get("来源") or fields.get("source"),
                "状态": fields.get("状态") or fields.get("status"),
                "备注": fields.get("备注") or fields.get("note") or fields.get("备注说明"),
            }
        )
    return out


def fetch_plan_table(*, page_size: int = 50) -> dict[str, Any]:
    app_token, table_id = ensure_plan_table()
    tok = _auth_token()
    n = max(5, min(int(page_size or 50), 100))
    payload = _http_json(
        "GET",
        f"/bitable/v1/apps/{urllib.parse.quote(str(app_token))}/tables/"
        f"{urllib.parse.quote(str(table_id))}/records",
        params={"page_size": n},
        token=tok,
    )
    data = payload.get("data") or {}
    items = data.get("items") or []
    slim = slim_plan_records(items)
    return {
        "source": "feishu_bitable_plan",
        "app_token": app_token,
        "table_id": table_id,
        "record_count": len(slim),
        "records": slim,
        "table_url_hint": f"https://feishu.cn/base/{app_token}",
        "resolved_via": "env_or_cache_or_auto_create",
    }


def add_plan_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """向近日计划表追加记录。每行可用键：日期/事项/优先级/来源/状态/备注。"""
    if not rows:
        raise ValueError("rows is empty")
    app_token, table_id = ensure_plan_table()
    tok = _auth_token()
    created: list[dict[str, Any]] = []
    for raw in rows[:20]:
        if not isinstance(raw, dict):
            continue
        fields: dict[str, Any] = {}
        mapping = {
            "日期": ("日期", "date"),
            "事项": ("事项", "title", "事项名称"),
            "优先级": ("优先级", "priority"),
            "来源": ("来源", "source"),
            "状态": ("状态", "status"),
            "备注": ("备注", "note", "备注说明"),
        }
        for canon, aliases in mapping.items():
            for a in aliases:
                if raw.get(a) not in (None, ""):
                    fields[canon] = raw.get(a)
                    break
        if not fields.get("事项"):
            continue
        if not fields.get("状态"):
            fields["状态"] = "待办"
        if not fields.get("来源"):
            fields["来源"] = "agent"
        payload = _http_json(
            "POST",
            f"/bitable/v1/apps/{urllib.parse.quote(str(app_token))}/tables/"
            f"{urllib.parse.quote(str(table_id))}/records",
            body={"fields": fields},
            token=tok,
        )
        rec = (payload.get("data") or {}).get("record") or {}
        created.append(
            {
                "record_id": rec.get("record_id"),
                **{k: fields.get(k) for k in ("日期", "事项", "优先级", "来源", "状态", "备注")},
            }
        )
    if not created:
        raise ValueError("No valid rows (need at least 事项)")
    return {
        "source": "feishu_bitable_plan_write",
        "app_token": app_token,
        "table_id": table_id,
        "created_count": len(created),
        "created": created,
        "table_url_hint": f"https://feishu.cn/base/{app_token}",
    }


def _message_text(msg: dict[str, Any]) -> str:
    body = msg.get("body") or {}
    content = body.get("content") or ""
    if isinstance(content, dict):
        return str(content.get("text") or content)[:500]
    s = str(content)
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return str(obj.get("text") or s)[:500]
    except Exception:
        pass
    return s[:500]


def _sender_name(sender: dict[str, Any]) -> str | None:
    """飞书消息里偶发带名称；多数情况只有 id，名称解析留到后续增强。"""
    for key in ("name", "sender_name", "user_name", "display_name"):
        val = sender.get(key)
        if val:
            return str(val).strip() or None
    return None


def slim_chat_messages(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in items:
        sender = m.get("sender") or {}
        sid = sender.get("id") or sender.get("sender_id")
        sname = _sender_name(sender)
        row: dict[str, Any] = {
            "message_id": m.get("message_id"),
            "msg_type": m.get("msg_type"),
            "create_time": _fmt_ts(m.get("create_time")),
            "sender_id": sid,
            "sender_type": sender.get("sender_type") or sender.get("id_type"),
            "text": _message_text(m),
        }
        if sname:
            row["sender_name"] = sname
        out.append(row)
    return out


# 信息卡字段契约（旁路提取器 / 文档用；长规则在 context.chat_extract.EXTRACT_SYSTEM_PROMPT）
CHAT_INFO_CARD_CONTRACT: dict[str, Any] = {
    "card_fields": {
        "kind": "summary | action | schedule | open_question",
        "title": "短标题",
        "detail": "有效说明写够细节（不为短而短）",
        "when": "时间提示，可空",
        "priority": "高|中|低，可空",
        "speakers": ["说话人；action/schedule/open_question 必填"],
        "evidence_message_ids": ["message_id"],
        "quote_hints": ["必填：3～5 个原文 verbatim 检索锚点（≤20 字/条）"],
    },
    "kind_meaning": {
        "summary": "结论/共识/规范",
        "action": "可执行待办",
        "schedule": "带明确时间的日程候选",
        "open_question": "未决，勿当成已确认待办",
    },
}


# 仅防 API 翻页死循环 / 异常刷屏的安全顶
_CHAT_INGEST_SAFETY_CEILING = 5000
_CHAT_RECENT_N_DEFAULT = 50
_CHAT_RECENT_N_MAX = 200


def fetch_chat_digest(
    *,
    hours: int = 48,
    max_messages: int | None = 0,
    recent_n: int = 0,
    chat_id: str | None = None,
) -> dict[str, Any]:
    """仅白名单会话：拉原始消息（未整理）进缓存。

    两种摄入模式（recent_n 优先）：
      - recent_n > 0：按「最近 N 条」拉（不按时间窗；适合不活跃群）
      - recent_n == 0：按 hours 时间窗拉；max_messages>0 时可再软截断
    身份由 FEISHU_CHAT_AUTH_MODE 控制（bot | user）。
    """
    allowed = feishu_plan_chat_ids()
    auth_mode = feishu_chat_auth_mode()
    if not allowed:
        raise RuntimeError(
            "Feishu chat digest not configured. Set FEISHU_PLAN_CHAT_IDS "
            "(comma-separated chat_ids). "
            "Auth: FEISHU_CHAT_AUTH_MODE=bot (default, app must join chats) "
            "or =user (you must be in chats + IM user scopes + oauth)."
        )
    if chat_id:
        cid = chat_id.strip()
        if cid not in allowed:
            raise RuntimeError(
                f"chat_id {cid!r} not in FEISHU_PLAN_CHAT_IDS whitelist: {allowed}"
            )
        targets = [cid]
    else:
        targets = allowed[:2]

    tok = _chat_access_token()
    use_recent = int(recent_n or 0) > 0
    if use_recent:
        want = max(1, min(int(recent_n), _CHAT_RECENT_N_MAX))
        soft_cap = True
        h = 0
        start = None
        end = None
        mode = "recent_n"
    else:
        h = max(1, min(int(hours or 48), 168))
        end = datetime.now(tz=_TZ_CN)
        start = end - timedelta(hours=h)
        if max_messages is None or int(max_messages) <= 0:
            want = _CHAT_INGEST_SAFETY_CEILING
            soft_cap = False
        else:
            want = max(1, min(int(max_messages), _CHAT_INGEST_SAFETY_CEILING))
            soft_cap = True
        mode = "hours"

    page_size = 50
    max_pages = max(1, (want + page_size - 1) // page_size) + 2
    digests = []
    for cid in targets:
        items: list[dict[str, Any]] = []
        page_token = None
        truncated = False
        hit_safety = False
        for _ in range(max_pages):
            params: dict[str, Any] = {
                "container_id_type": "chat",
                "container_id": cid,
                "page_size": page_size,
                "sort_type": "ByCreateTimeDesc",
            }
            if not use_recent and start is not None and end is not None:
                params["start_time"] = str(int(start.timestamp()))
                params["end_time"] = str(int(end.timestamp()))
            if page_token:
                params["page_token"] = page_token
            payload = _http_json(
                "GET",
                "/im/v1/messages",
                params=params,
                token=tok,
            )
            data = payload.get("data") or {}
            batch = data.get("items") or []
            items.extend(batch)
            if len(items) >= want:
                items = items[:want]
                truncated = True
                hit_safety = not soft_cap
                break
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
            if not page_token:
                break
        digests.append(
            {
                "chat_id": cid,
                "message_count": len(items),
                "truncated": truncated,
                "hit_safety_ceiling": hit_safety,
                "messages": slim_chat_messages(items),
            }
        )

    if use_recent:
        policy = (
            f"Newest-first recent_n={want} (no time window); "
            f"cap={_CHAT_RECENT_N_MAX}."
        )
        range_start = ""
        range_end = ""
        # 从实际消息时间填展示用区间（若有）
        times: list[str] = []
        for d in digests:
            for m in d.get("messages") or []:
                t = (m.get("create_time") or "").strip()
                if t:
                    times.append(t)
        if times:
            times_sorted = sorted(times)
            range_start = times_sorted[0]
            range_end = times_sorted[-1]
    elif soft_cap:
        policy = (
            f"Within last {h}h, newest-first; optional soft cap max_messages={want} "
            f"(truncated if more in window)."
        )
        assert start is not None and end is not None
        range_start = start.strftime("%Y-%m-%d %H:%M")
        range_end = end.strftime("%Y-%m-%d %H:%M")
    else:
        policy = (
            f"Within last {h}h, fetch all newest-first (no product message cap); "
            f"safety ceiling={_CHAT_INGEST_SAFETY_CEILING} only."
        )
        assert start is not None and end is not None
        range_start = start.strftime("%Y-%m-%d %H:%M")
        range_end = end.strftime("%Y-%m-%d %H:%M")

    return {
        "source": "feishu_chat_digest",
        "ingest_mode": mode,
        "hours": h,
        "recent_n": want if use_recent else 0,
        "max_messages": want if soft_cap and not use_recent else (want if use_recent else 0),
        "safety_ceiling": _CHAT_INGEST_SAFETY_CEILING,
        "auth_mode": auth_mode,
        "range_start": range_start,
        "range_end": range_end,
        "ingest_policy": policy,
        "whitelist": allowed,
        "chats": digests,
        "note": (
            "RAW messages for session cache + bypass extract. "
            f"auth_mode={auth_mode} ingest_mode={mode}. "
            "Use recent_n for quiet groups; hours for active time windows."
        ),
    }
