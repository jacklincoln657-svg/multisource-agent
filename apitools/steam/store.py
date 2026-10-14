"""Steam 商店只读查询（公开 storefront JSON，无需 API Key）。

标准「商店卡片」字段（给 Agent 展开回复用，勿自行发明价评）：
  appid, name, genres, short_description, store_url, price,
  review_label, positive_pct, total_reviews, metacritic

  https://store.steampowered.com/api/storesearch/
  https://store.steampowered.com/api/appdetails
  https://store.steampowered.com/appreviews/{appid}
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

STORE_SEARCH = "https://store.steampowered.com/api/storesearch/"
STORE_DETAILS = "https://store.steampowered.com/api/appdetails"
STORE_REVIEWS = "https://store.steampowered.com/appreviews"
_UA = "cyber_lobster_api_fusion/0.3"

# Agent 可见的基础卡片键（回复应在此基础上展开）
STORE_CARD_KEYS = (
    "appid",
    "name",
    "genres",
    "short_description",
    "store_url",
    "price",
    "review_label",
    "positive_pct",
    "total_reviews",
    "metacritic",
)

_REVIEW_DESC_ZH = {
    "Overwhelmingly Positive": "好评如潮",
    "Very Positive": "特别好评",
    "Mostly Positive": "多半好评",
    "Positive": "好评",
    "Mixed": "褒贬不一",
    "Mostly Negative": "多半差评",
    "Negative": "差评",
    "Very Negative": "特别差评",
    "Overwhelmingly Negative": "差评如潮",
    "No user reviews": "暂无评测",
}


def _http_json(url: str, timeout: float = 20.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"Steam Store HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Steam Store network error: {e.reason}") from e
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Steam Store response is not JSON: {e}") from e


def _format_price(price_final: Any, currency: str, is_free: bool) -> Optional[str]:
    if is_free:
        return "free"
    if price_final is None:
        return None
    try:
        return f"{currency or 'CNY'} {int(price_final) / 100:.2f}"
    except (TypeError, ValueError):
        return None


def to_store_card(row: dict[str, Any]) -> dict[str, Any]:
    """把任意 enrich 行压成标准商店卡片。"""
    return {
        "appid": row.get("appid"),
        "name": row.get("name"),
        "genres": (row.get("genres") or [])[:3],
        "short_description": (row.get("short_description") or "")[:120],
        "store_url": row.get("store_url")
        or (f"https://store.steampowered.com/app/{row['appid']}/" if row.get("appid") else None),
        "price": row.get("price")
        if "price" in row and isinstance(row.get("price"), str)
        else _format_price(row.get("price_final"), row.get("price_currency") or "CNY", bool(row.get("is_free"))),
        "review_label": row.get("review_label"),
        "positive_pct": row.get("positive_pct"),
        "total_reviews": row.get("total_reviews"),
        "metacritic": row.get("metacritic"),
    }


def store_search(
    query: str,
    *,
    max_results: int = 8,
    lang: str = "schinese",
    cc: str = "CN",
) -> list[dict[str, Any]]:
    """按关键词搜商店，返回精简候选（不含已过滤逻辑）。"""
    q = (query or "").strip()
    if not q:
        raise ValueError("query is empty")
    n = max(1, min(int(max_results or 8), 15))
    qs = urllib.parse.urlencode({"term": q, "l": lang, "cc": cc})
    raw = _http_json(f"{STORE_SEARCH}?{qs}")
    items = list((raw or {}).get("items") or [])
    out: list[dict[str, Any]] = []
    for it in items:
        if it.get("type") and it.get("type") != "app":
            # 推荐优先本体；DLC/通票留给 lookup 或用户点名
            continue
        appid = it.get("id")
        if appid is None:
            continue
        price = it.get("price") or {}
        out.append(
            {
                "appid": int(appid),
                "name": it.get("name") or f"app_{appid}",
                "type": it.get("type") or "app",
                "tiny_image": it.get("tiny_image"),
                "price_final": price.get("final"),
                "price_currency": price.get("currency"),
                "store_url": f"https://store.steampowered.com/app/{int(appid)}/",
            }
        )
        if len(out) >= n:
            break
    # 若过滤 type 后太少，回退用原始列表前 n 条
    if not out:
        for it in items[:n]:
            appid = it.get("id")
            if appid is None:
                continue
            price = it.get("price") or {}
            out.append(
                {
                    "appid": int(appid),
                    "name": it.get("name") or f"app_{appid}",
                    "type": it.get("type"),
                    "tiny_image": it.get("tiny_image"),
                    "price_final": price.get("final"),
                    "price_currency": price.get("currency"),
                    "store_url": f"https://store.steampowered.com/app/{int(appid)}/",
                }
            )
    return out


def fetch_app_details(
    appids: list[int],
    *,
    lang: str = "schinese",
    cc: str = "CN",
) -> dict[int, dict[str, Any]]:
    """逐个拉 appdetails（可并行调用本函数的外层）。"""
    ids = [int(a) for a in appids if a is not None][:8]
    result: dict[int, dict[str, Any]] = {}

    def _one(aid: int) -> tuple[int, Optional[dict[str, Any]]]:
        qs = urllib.parse.urlencode({"appids": str(aid), "l": lang, "cc": cc})
        try:
            raw = _http_json(f"{STORE_DETAILS}?{qs}")
        except Exception:
            return aid, None
        if not isinstance(raw, dict):
            return aid, None
        block = raw.get(str(aid)) or raw.get(aid)
        if not block or not block.get("success"):
            return aid, None
        data = block.get("data") or {}
        genres = [g.get("description") for g in (data.get("genres") or []) if g.get("description")]
        cats = [c.get("description") for c in (data.get("categories") or []) if c.get("description")]
        meta = data.get("metacritic") or {}
        price_overview = data.get("price_overview") or {}
        return aid, {
            "appid": aid,
            "name": data.get("name"),
            "short_description": (data.get("short_description") or "")[:280],
            "genres": genres[:8],
            "categories": cats[:10],
            "release_date": (data.get("release_date") or {}).get("date"),
            "is_free": bool(data.get("is_free")),
            "metacritic": meta.get("score"),
            "price_final": price_overview.get("final"),
            "price_currency": price_overview.get("currency"),
            "store_url": f"https://store.steampowered.com/app/{aid}/",
        }

    if not ids:
        return result
    workers = min(6, len(ids))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_one, aid) for aid in ids]
        for fut in as_completed(futs):
            aid, row = fut.result()
            if row:
                result[aid] = row
    return result


def fetch_review_summary(appid: int) -> dict[str, Any]:
    """只取评测摘要（评分档位 + 好评占比），不拉长评正文。"""
    qs = urllib.parse.urlencode(
        {
            "json": 1,
            "language": "all",
            "purchase_type": "all",
            "num_per_page": 0,
            "filter": "summary",
        }
    )
    raw = _http_json(f"{STORE_REVIEWS}/{int(appid)}?{qs}")
    if not isinstance(raw, dict) or raw.get("success") != 1:
        return {}
    qs_sum = raw.get("query_summary") or {}
    total = int(qs_sum.get("total_reviews") or 0)
    pos = int(qs_sum.get("total_positive") or 0)
    desc_en = (qs_sum.get("review_score_desc") or "").strip()
    pct = round(100.0 * pos / total, 1) if total > 0 else None
    return {
        "review_score": qs_sum.get("review_score"),
        "review_label": _REVIEW_DESC_ZH.get(desc_en, desc_en or None),
        "review_label_en": desc_en or None,
        "positive_pct": pct,
        "total_reviews": total if total else None,
    }


def _enrich_hit(hit: dict[str, Any], detail: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    merged = dict(hit)
    if detail:
        for k in (
            "short_description",
            "genres",
            "categories",
            "release_date",
            "is_free",
            "metacritic",
            "name",
            "store_url",
        ):
            if detail.get(k) is not None:
                merged[k] = detail[k]
        if detail.get("price_final") is not None:
            merged["price_final"] = detail["price_final"]
            merged["price_currency"] = detail.get("price_currency") or merged.get("price_currency")
    if review:
        merged.update(review)
    return merged


def lookup_app(
    appid: int,
    *,
    lang: str = "schinese",
    cc: str = "CN",
) -> dict[str, Any]:
    """按 appid 拉一张标准商店卡片（详情 + 评测并行）。"""
    aid = int(appid)

    def _details() -> dict[str, Any]:
        return fetch_app_details([aid], lang=lang, cc=cc).get(aid) or {}

    def _reviews() -> dict[str, Any]:
        try:
            return fetch_review_summary(aid)
        except Exception:
            return {}

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_d = pool.submit(_details)
        f_r = pool.submit(_reviews)
        detail = f_d.result()
        review = f_r.result()

    base = {
        "appid": aid,
        "name": (detail or {}).get("name") or f"app_{aid}",
        "store_url": f"https://store.steampowered.com/app/{aid}/",
        "price_final": (detail or {}).get("price_final"),
        "price_currency": (detail or {}).get("price_currency"),
        "is_free": (detail or {}).get("is_free"),
    }
    full = _enrich_hit(base, detail, review)
    return {
        "source": "steam_store_lookup",
        "appid": aid,
        "result": full,
        "card": to_store_card(full),
    }


def search_and_enrich(
    query: str,
    *,
    max_results: int = 8,
    exclude_appids: Optional[set[int]] = None,
    with_details: bool = True,
    with_reviews: bool = True,
) -> dict[str, Any]:
    """搜索 → 排除已有 → 并行补详情/评测 → 标准卡片列表。"""
    exclude = {int(x) for x in (exclude_appids or set())}
    hits = store_search(query, max_results=max(max_results * 2, 10))
    filtered = [h for h in hits if h["appid"] not in exclude][: max(1, min(max_results, 12))]
    if not filtered:
        return {
            "source": "steam_store",
            "query": query,
            "excluded_owned_count": len(exclude),
            "results": [],
            "cards": [],
        }

    details: dict[int, dict[str, Any]] = {}
    reviews: dict[int, dict[str, Any]] = {}
    ids = [h["appid"] for h in filtered]

    if with_details:
        details = fetch_app_details(ids)

    if with_reviews:
        def _rev(aid: int) -> tuple[int, dict[str, Any]]:
            try:
                return aid, fetch_review_summary(aid)
            except Exception:
                return aid, {}

        with ThreadPoolExecutor(max_workers=min(6, len(ids))) as pool:
            for aid, rev in pool.map(lambda a: _rev(a), ids):
                reviews[aid] = rev

    enriched = []
    for h in filtered:
        aid = h["appid"]
        enriched.append(_enrich_hit(h, details.get(aid) or {}, reviews.get(aid) or {}))

    cards = [to_store_card(r) for r in enriched]
    return {
        "source": "steam_store",
        "query": query,
        "excluded_owned_count": len(exclude),
        "results": enriched,
        "cards": cards,
    }


def slim_results_for_agent(payload: dict[str, Any]) -> dict[str, Any]:
    """给模型的瘦身视图：只返回标准商店卡片。"""
    cards = payload.get("cards")
    if not cards:
        cards = [to_store_card(r) for r in (payload.get("results") or [])]
    out: dict[str, Any] = {
        "source": payload.get("source") or "steam_store",
        "cards": cards,
        "card_fields": list(STORE_CARD_KEYS),
    }
    if payload.get("query") is not None:
        out["query"] = payload.get("query")
    if payload.get("appid") is not None:
        out["appid"] = payload.get("appid")
    if payload.get("excluded_owned_count") is not None:
        out["excluded_owned_count"] = payload.get("excluded_owned_count")
    return out
