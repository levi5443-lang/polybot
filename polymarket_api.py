"""
polymarket_api.py — shared data-fetching helpers for the consensus bot + backtester.

Verify field names against current docs (https://docs.polymarket.com) — these
are the commonly-seen shapes for the Data API and Gamma API as of mid-2026,
but prediction-market platforms iterate their APIs fairly often.
"""

import time
import requests

DATA_API_BASE = "https://data-api.polymarket.com"
GAMMA_API_BASE = "https://gamma-api.polymarket.com"  # market metadata / resolutions


def fetch_leaderboard(period: str = "30d", limit: int = 20) -> list[str]:
    """Top wallets by PnL/volume for the given period ('1d'|'7d'|'30d'|'all')."""
    resp = requests.get(
        f"{DATA_API_BASE}/v1/leaderboard", params={"period": period, "limit": limit}, timeout=15
    )
    resp.raise_for_status()
    data = resp.json()
    wallets = []
    for entry in data:
        wallet = entry.get("proxyWallet") or entry.get("wallet") or entry.get("address")
        if wallet:
            wallets.append(wallet)
    return wallets[:limit]


def fetch_positions(wallet: str) -> list[dict]:
    """Current open positions for a wallet. Returns raw dicts (caller parses fields)."""
    resp = requests.get(f"{DATA_API_BASE}/positions", params={"user": wallet}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def fetch_trades(wallet: str = None, market: str = None, limit: int = 500) -> list[dict]:
    """Historical fills, optionally filtered by wallet and/or market (conditionId).

    Used by the backtester to reconstruct what a wallet's position looked like
    at an arbitrary point in time, since /positions only gives current state.
    """
    params = {"limit": limit}
    if wallet:
        params["user"] = wallet
    if market:
        params["market"] = market
    resp = requests.get(f"{DATA_API_BASE}/trades", params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def fetch_resolved_markets(limit: int = 100, offset: int = 0) -> list[dict]:
    """Closed/resolved markets with their final outcome, for backtesting."""
    resp = requests.get(
        f"{GAMMA_API_BASE}/markets",
        params={"closed": "true", "limit": limit, "offset": offset},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


PREFERRED_CATEGORY_LABELS = [
    "Politics", "Sports", "Crypto", "Pop Culture", "Business",
    "Science", "Entertainment", "Elections", "Economy", "Weather",
]


def fetch_event_categories(event_ids: list[str]) -> dict[str, str]:
    """Look up each event's category via the Gamma API's dedicated
    /events/{id}/tags endpoint (confirmed working against live data —
    the /markets?condition_ids=... approach silently returns nothing).

    Returns {event_id: category_label}. An event, e.g. an EPL match, is
    usually tagged with several labels of different specificity (e.g.
    "FA Community Shield", "Sports", "Soccer") — this prefers a broad,
    known top-level category (PREFERRED_CATEGORY_LABELS) over a narrow
    one, so filter tabs stay small and useful. Falls back to whatever
    the first returned tag is if none of the preferred labels are present.
    """
    if not event_ids:
        return {}

    unique_ids = list(dict.fromkeys(str(e) for e in event_ids if e))
    categories: dict[str, str] = {}

    for event_id in unique_ids:
        try:
            resp = requests.get(f"{GAMMA_API_BASE}/events/{event_id}/tags", timeout=15)
            resp.raise_for_status()
            tags = resp.json() or []
            labels = [t.get("label") for t in tags if isinstance(t, dict) and t.get("label")]

            chosen = next((lbl for lbl in PREFERRED_CATEGORY_LABELS if lbl in labels), None)
            categories[event_id] = chosen or (labels[0] if labels else "Uncategorized")
        except requests.RequestException:
            categories[event_id] = "Uncategorized"
        time.sleep(0.05)

    return categories


def polite_sleep(seconds: float = 0.2):
    time.sleep(seconds)
