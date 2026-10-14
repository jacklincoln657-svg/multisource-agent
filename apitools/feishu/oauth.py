"""飞书用户 OAuth：授权码换 token、refresh、本地缓存。"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

FEISHU_API = "https://open.feishu.cn/open-apis"
AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
TOKEN_URL = f"{FEISHU_API}/authen/v2/oauth/token"
_UA = "cyber_lobster_api_fusion/0.5 (+feishu-oauth)"
_TZ_CN = timezone(timedelta(hours=8))

# 日历读写 + 多维表格 + 群聊消息只读（用户身份）+ offline（拿 refresh_token）
# 开放平台须在「用户身份权限」开通对应项；改权限后必须重新跑 scripts/feishu_oauth_login.py
# 读历史：优先 im:message:readonly（开放平台常见）；im:message / im:message.history:readonly 亦可。
# 可用 FEISHU_OAUTH_SCOPES 整段覆盖默认列表（旧 token 不会自动带新 scope）。
DEFAULT_SCOPES = (
    "offline_access "
    "calendar:calendar:readonly "
    "calendar:calendar "
    "calendar:calendar.event:create "
    "bitable:app "
    "base:app:create "
    "im:message:readonly"
)

DEFAULT_REDIRECT_URI = "http://127.0.0.1:8765/callback"
DEFAULT_PORT = 8765

# refresh_token 只能用一次；并行工具调用必须串行刷新
_token_lock = threading.Lock()


def _load_env() -> None:
    from util.env import load_project_env

    load_project_env()


def _workspace_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "workspace"


def token_cache_path() -> Path:
    return _workspace_dir() / "feishu_user_token.json"


def redirect_uri() -> str:
    _load_env()
    return (os.getenv("FEISHU_OAUTH_REDIRECT_URI") or DEFAULT_REDIRECT_URI).strip()


def oauth_scopes() -> str:
    _load_env()
    raw = (os.getenv("FEISHU_OAUTH_SCOPES") or "").strip()
    return raw or DEFAULT_SCOPES.strip()


def app_credentials() -> tuple[str, str]:
    _load_env()
    app_id = (os.getenv("FEISHU_APP_ID") or "").strip()
    app_secret = (os.getenv("FEISHU_APP_SECRET") or "").strip()
    if not app_id or not app_secret:
        raise RuntimeError("Missing FEISHU_APP_ID / FEISHU_APP_SECRET in project root .env")
    return app_id, app_secret


def build_authorize_url(*, state: str | None = None) -> tuple[str, str]:
    app_id, _ = app_credentials()
    st = state or secrets.token_urlsafe(16)
    params = {
        "client_id": app_id,
        "response_type": "code",
        "redirect_uri": redirect_uri(),
        "scope": oauth_scopes(),
        "state": st,
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}", st


def _post_token(body: dict[str, Any]) -> dict[str, Any]:
    raw_body = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL,
        data=raw_body,
        headers={
            "User-Agent": _UA,
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Feishu OAuth HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Feishu OAuth network error: {e.reason}") from e

    code = payload.get("code")
    if code not in (0, None):
        raise RuntimeError(
            f"Feishu OAuth error code={code} msg={payload.get('error_description') or payload.get('msg') or payload}"
        )
    # v2 成功时 token 在顶层；偶发包在 data
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    access = data.get("access_token")
    if not access:
        raise RuntimeError(f"Feishu OAuth missing access_token: {payload}")
    return data


def exchange_code_for_token(code: str) -> dict[str, Any]:
    app_id, app_secret = app_credentials()
    data = _post_token(
        {
            "grant_type": "authorization_code",
            "client_id": app_id,
            "client_secret": app_secret,
            "code": code,
            "redirect_uri": redirect_uri(),
        }
    )
    return save_token_bundle(data, source="authorization_code")


def refresh_user_token(refresh_token: str) -> dict[str, Any]:
    app_id, app_secret = app_credentials()
    data = _post_token(
        {
            "grant_type": "refresh_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "refresh_token": refresh_token,
        }
    )
    return save_token_bundle(data, source="refresh_token")


def save_token_bundle(data: dict[str, Any], *, source: str) -> dict[str, Any]:
    now = time.time()
    expires_in = int(data.get("expires_in") or 7200)
    refresh_expires_in = data.get("refresh_token_expires_in")
    bundle = {
        "access_token": data.get("access_token"),
        "refresh_token": data.get("refresh_token"),
        "token_type": data.get("token_type") or "Bearer",
        "scope": data.get("scope"),
        "expires_in": expires_in,
        "expires_at": now + expires_in,
        "refresh_token_expires_in": refresh_expires_in,
        "refresh_expires_at": (
            now + int(refresh_expires_in) if refresh_expires_in is not None else None
        ),
        "updated_at": datetime.now(tz=_TZ_CN).strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
    }
    # 刷新时若未返回新 refresh_token，保留旧的（按文档应返回新的且旧的作废）
    if not bundle.get("refresh_token"):
        old = load_token_bundle()
        if old and old.get("refresh_token"):
            bundle["refresh_token"] = old["refresh_token"]
            bundle["refresh_expires_at"] = old.get("refresh_expires_at")

    path = token_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    return bundle


def load_token_bundle() -> Optional[dict[str, Any]]:
    path = token_cache_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("access_token"):
        return None
    return data


def get_valid_user_access_token(*, force_refresh: bool = False) -> Optional[str]:
    """
    返回可用的 user_access_token：
    1) workspace/feishu_user_token.json（可自动 refresh，线程安全）
    2) 回退 .env FEISHU_USER_ACCESS_TOKEN（无 refresh）
    """
    _load_env()
    with _token_lock:
        bundle = load_token_bundle()
        if bundle:
            expires_at = float(bundle.get("expires_at") or 0)
            access = str(bundle.get("access_token") or "").strip()
            refresh = str(bundle.get("refresh_token") or "").strip()
            # 提前 120s 刷新
            if access and not force_refresh and time.time() < expires_at - 120:
                return access
            if refresh:
                # 进锁后再读一次：并行调用时可能已被别的线程刷新成功
                bundle2 = load_token_bundle()
                if bundle2 and not force_refresh:
                    exp2 = float(bundle2.get("expires_at") or 0)
                    acc2 = str(bundle2.get("access_token") or "").strip()
                    if acc2 and time.time() < exp2 - 120:
                        return acc2
                    refresh = str(bundle2.get("refresh_token") or refresh).strip()
                try:
                    refreshed = refresh_user_token(refresh)
                    return str(refreshed.get("access_token") or "").strip() or None
                except Exception:
                    # 再读盘：可能并发刷新已成功，旧 refresh 失效但新 access 已写入
                    bundle3 = load_token_bundle()
                    if bundle3:
                        exp3 = float(bundle3.get("expires_at") or 0)
                        acc3 = str(bundle3.get("access_token") or "").strip()
                        if acc3 and time.time() < exp3 - 60:
                            return acc3
                    if access and time.time() < expires_at:
                        return access
                    raise
            if access and time.time() < expires_at:
                return access

        env_tok = (os.getenv("FEISHU_USER_ACCESS_TOKEN") or "").strip()
        return env_tok or None
