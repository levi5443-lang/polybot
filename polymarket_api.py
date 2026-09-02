"""
polymarket_api.py — shared data-fetching helpers for the consensus bot + backtester.

Verify field names against current docs (https://docs.polymarket.com) — these
are the commonly-seen shapes for the Data API and Gamma API as of mid-2026,
but prediction-market platforms iterate their APIs fairly often.
"""

import time
import logging
import json
import os
import requests

log = logging.getLogger("polymarket_api")

DATA_API_BASE = "https://data-api.polymarket.com"
GAMMA_API_BASE = "https://gamma-api.polymarket.com"

# Event categories basically never change once assigned, so cache them to
# disk (not just in-memory) — that way every run after the first one, even
# a brand-new `python consensus_bot.py` invocation, skips re-fetching
# categories for events it's already seen.
CACHE_DIR = os.environ.get("CACHE_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(CACHE_DIR, exist_ok=True)
CATEGORY_CACHE_FILE = os.path.join(CACHE_DIR, "category_cache.json")


def _load_category_cache() -> dict:
    if os.path.exists(CATEGORY_CACHE_FILE):
        try:
            with open(CATEGORY_CACHE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_category_cache(cache: dict) -> None:
    try:
        with open(CATEGORY_CACHE_FILE, "w") as f:
            json.dump(cache, f)
    except OSError:
        pass  # caching is a nice-to-have — never let a write failure be fatal  # market metadata / resolutions


# The Data API's actual query params (per docs.polymarket.com/api-reference/
# core/get-trader-leaderboard-rankings) are timePeriod (DAY|WEEK|MONTH|ALL),
# orderBy (PNL|VOL, default PNL), limit (max 50 per call), and offset — NOT
# the plain "period"/"limit" this used to send. Those were silently ignored,
# which meant two things were quietly broken: LEADERBOARD_PERIOD never
# actually took effect (ranking always fell back to the API's DAY default),
# and CANDIDATE_POOL_SIZE=100 was always being truncated to 50 by the API's
# per-call cap — which is exactly why the deployed bot's logs say "Got 50
# candidate wallets" even though 100 was requested.
_TIME_PERIOD_MAP = {"1d": "DAY", "7d": "WEEK", "30d": "MONTH", "all": "ALL"}
_LEADERBOARD_PAGE_SIZE = 50


def fetch_leaderboard(period: str = "30d", limit: int = 20) -> list[str]:
    """Top wallets by profit (PnL) for the given period ('1d'|'7d'|'30d'|'all').

    Explicitly ranks by PnL (orderBy=PNL) rather than trusting the API's
    default — a high-volume wallet can still be a net loser, and "top
    trader" should mean winning, not just active. Pages through results in
    batches of 50 (the API's per-call max) so a `limit` above that, like
    CANDIDATE_POOL_SIZE, is actually honored instead of silently capped.
    """
    time_period = _TIME_PERIOD_MAP.get(period, "MONTH")
    wallets: list[str] = []
    offset = 0
    while len(wallets) < limit:
        page_limit = min(_LEADERBOARD_PAGE_SIZE, limit - len(wallets))
        resp = requests.get(
            f"{DATA_API_BASE}/v1/leaderboard",
            params={
                "timePeriod": time_period,
                "orderBy": "PNL",
                "limit": page_limit,
                "offset": offset,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break  # fewer wallets exist than requested — nothing more to page in
        for entry in data:
            wallet = entry.get("proxyWallet") or entry.get("wallet") or entry.get("address")
            if wallet:
                wallets.append(wallet)
        offset += page_limit
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
    /events/{id}/tags endpoint. Checks the local cache first (see
    CATEGORY_CACHE_FILE) and only hits the network for events not already
    known — categories basically never change once assigned, so this
    persists across separate script runs, not just within one process.

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
    cache = _load_category_cache()

    categories: dict[str, str] = {}
    to_fetch = []
    for event_id in unique_ids:
        if event_id in cache:
            categories[event_id] = cache[event_id]
        else:
            to_fetch.append(event_id)

    log.info("Category cache: %d/%d already known, fetching %d new",
              len(unique_ids) - len(to_fetch), len(unique_ids), len(to_fetch))

    for i, event_id in enumerate(to_fetch, start=1):
        if i % 10 == 0 or i == len(to_fetch):
            log.info("  ...categories: %d/%d new events done", i, len(to_fetch))
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

    if to_fetch:
        cache.update({eid: categories[eid] for eid in to_fetch})
        _save_category_cache(cache)

    return categories


def polite_sleep(seconds: float = 0.2):
    time.sleep(seconds)
