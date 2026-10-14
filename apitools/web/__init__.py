"""网页证据：Tavily Search + 受控拉页。"""

from apitools.web.client import (
    EVIDENCE_CARD_KEYS,
    slim_evidence_payload,
    tavily_configured,
    web_fetch,
    web_search,
    web_search_configured,
)

__all__ = [
    "EVIDENCE_CARD_KEYS",
    "tavily_configured",
    "web_search_configured",
    "web_search",
    "web_fetch",
    "slim_evidence_payload",
]
