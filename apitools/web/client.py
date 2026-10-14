"""网页证据：Tavily Search + 受控拉页（web_fetch）。"""
from __future__ import annotations

import gzip
import json
import os
import re
import urllib.error
import urllib.request
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

_UA = "cyber_lobster_api_fusion/0.4 (+personal-agent)"
TAVILY_ENDPOINT = "https://api.tavily.com/search"
EVIDENCE_CARD_KEYS = ("title", "url", "snippet", "site", "kind")
_BLOCKED_HOSTS = {"127.0.0.1", "0.0.0.0", "::1", "localhost"}


def _load_env() -> None:
    from util.env import load_project_env

    load_project_env()


def tavily_configured() -> bool:
    _load_env()
    return bool((os.getenv("TAVILY_API_KEY") or "").strip())


def web_search_configured() -> bool:
    return tavily_configured()


def _allowlist() -> set[str]:
    _load_env()
    raw = (os.getenv("WEB_FETCH_ALLOWLIST") or "").strip()
    if not raw:
        return set()
    return {x.strip().lower().lstrip(".") for x in raw.split(",") if x.strip()}


def _host_ok(host: str, *, for_fetch: bool) -> bool:
    h = (host or "").lower().strip(".")
    if not h or h in _BLOCKED_HOSTS or h.endswith(".local"):
        return False
    if re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[0-1])\.)", h):
        return False
    allow = _allowlist()
    if for_fetch and allow:
        return any(h == a or h.endswith("." + a) for a in allow)
    return True


def _http_get(
    url: str,
    *,
    timeout: float = 20.0,
    headers: dict[str, str] | None = None,
) -> tuple[bytes, str]:
    hdrs = {
        "User-Agent": _UA,
        "Accept": "application/json,text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            return resp.read(), ctype
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"network error: {e.reason}") from e


def _http_post_json(
    url: str,
    body: dict[str, Any],
    *,
    timeout: float = 30.0,
    headers: dict[str, str] | None = None,
) -> Any:
    hdrs = {
        "User-Agent": _UA,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if headers:
        hdrs.update(headers)
    raw_body = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=raw_body, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _decode_json_body(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"network error: {e.reason}") from e


def _decode_json_body(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        try:
            return json.loads(gzip.decompress(raw).decode("utf-8", errors="replace"))
        except Exception as e:
            raise RuntimeError(f"response not JSON: {e}") from e


def to_evidence_card(*, title: str, url: str, snippet: str, kind: str) -> dict[str, Any]:
    site = (urlparse(url).netloc or "").lower()
    limit = 420 if kind == "search_snippet" else 2800
    return {
        "title": (title or site or "untitled")[:160],
        "url": url,
        "snippet": (snippet or "")[:limit],
        "site": site,
        "kind": kind,
    }


def slim_evidence_payload(cards: list[dict[str, Any]], *, query: str = "") -> dict[str, Any]:
    return {
        "source": "web_evidence",
        "query": query or None,
        "card_fields": list(EVIDENCE_CARD_KEYS),
        "cards": cards,
        "usage_note": (
            "外部事实只能依据 cards[].snippet；冲突则并列来源并标不确定；"
            "禁止用训练记忆补版本号/日期；禁止维基朗读与同义反复；"
            "用户要详细或多主题时按话题写透，有料就展开，勿人为压短。"
        ),
    }


def web_search(query: str, *, max_results: int = 5) -> dict[str, Any]:
    q = (query or "").strip()
    if not q:
        raise ValueError("query is empty")
    n = max(1, min(int(max_results or 5), 8))
    _load_env()
    key = (os.getenv("TAVILY_API_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "Set TAVILY_API_KEY in project root .env (https://app.tavily.com/home)."
        )
    data = _http_post_json(
        TAVILY_ENDPOINT,
        {
            "query": q,
            "max_results": n,
            "search_depth": "basic",
            "include_answer": False,
        },
        headers={"Authorization": f"Bearer {key}"},
    )
    cards: list[dict[str, Any]] = []
    for item in data.get("results") or []:
        link = (item.get("url") or "").strip()
        if not link.startswith("http"):
            continue
        host = urlparse(link).hostname or ""
        if not _host_ok(host, for_fetch=False):
            continue
        cards.append(
            to_evidence_card(
                title=(item.get("title") or "").strip(),
                url=link,
                snippet=(item.get("content") or "").strip(),
                kind="search_snippet",
            )
        )
    if not cards:
        raise RuntimeError("Tavily returned 0 usable results; try another query.")
    return {"query": q, "engine": "tavily", "cards": cards}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._chunks: list[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in {"script", "style", "noscript", "svg", "iframe", "nav", "footer", "header"}:
            self._skip += 1
        if t == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in {"script", "style", "noscript", "svg", "iframe", "nav", "footer", "header"}:
            self._skip = max(0, self._skip - 1)
        if t == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self._skip:
            return
        s = data.strip()
        if s:
            self._chunks.append(s)


def html_to_text(page: str, max_chars: int = 2800) -> tuple[str, str]:
    parser = _TextExtractor()
    try:
        parser.feed(page)
        parser.close()
    except Exception:
        pass
    title = re.sub(r"\s+", " ", parser.title).strip()
    body = re.sub(r"\s+", " ", " ".join(parser._chunks)).strip()
    if len(body) > max_chars:
        body = body[: max_chars - 1] + "…"
    return title, body


def web_fetch(url: str, *, max_chars: int = 2800) -> dict[str, Any]:
    u = (url or "").strip()
    if not u.startswith(("http://", "https://")):
        raise ValueError("url must be http(s)")
    host = urlparse(u).hostname or ""
    if not _host_ok(host, for_fetch=True):
        allow = _allowlist()
        if allow:
            raise RuntimeError(f"host '{host}' not in WEB_FETCH_ALLOWLIST")
        raise RuntimeError(f"host '{host}' is blocked")

    raw, ctype = _http_get(u)
    if ctype and not any(x in ctype for x in ("html", "text", "xml", "json")):
        raise RuntimeError(f"unsupported content-type: {ctype}")
    text = raw.decode("utf-8", errors="replace")
    if "json" in (ctype or "") and text.lstrip().startswith(("{", "[")):
        title, body = host, text[:max_chars]
    else:
        title, body = html_to_text(text, max_chars=max_chars)
    if not body:
        raise RuntimeError("no extractable text")
    card = to_evidence_card(title=title or host, url=u, snippet=body, kind="page_excerpt")
    return {"url": u, "engine": "web_fetch", "cards": [card], "card": card}
