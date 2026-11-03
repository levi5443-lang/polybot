"""
polymarket_api.py — shared data-fetching helpers for the consensus bot + backtester.

Verify field names against current docs (https://docs.polymarket.com) — these
are the commonly-seen shapes for the Data API and Gamma API as of mid-2026,
but prediction-market platforms iterate their APIs fairly often.

DATA API V1 -> V2 (2026-11-03): Polymarket retired Data API v1 — every
unversioned or /v1/-prefixed path on data-api.polymarket.com (leaderboard,
positions, trades, among others) — in favor of v2. It silently stopped
working here sometime between 2026-11-02 20:15 and 20:36 UTC (410 Gone on
every call), and because the one caller that wraps these in a try/except
(category_leaderboard.py) just logs a warning rather than crashing, nothing
visibly broke for roughly 41 hours — the bot just stopped finding any
candidate wallets at all, so no signal of any kind (paper or live) could
fire in that window. fetch_leaderboard() and fetch_positions() below are
migrated to v2 and confirmed against live responses; fetch_trades() is
migrated to the v2 path/params but its per-trade field names are NOT yet
confirmed against the backtester that consumes it (see that function's own
docstring). GAMMA_API_BASE and CLOB_API_BASE calls elsewhere in this file
are a separate Polymarket product from the Data API and were not affected.
"""

import time
import logging
import requests

log = logging.getLogger("polymarket_api")

DATA_API_BASE = "https://data-api.polymarket.com"
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"

# Event categories basically never change once assigned, so cache them —
# shared across both services (worker + dashboard), not just local to
# whichever one happens to look one up first.
from shared_storage import get_json, set_json

CATEGORY_CACHE_KEY = "category_cache.json"


def _load_category_cache() -> dict:
    return get_json(CATEGORY_CACHE_KEY, {})


def _save_category_cache(cache: dict) -> None:
    set_json(CATEGORY_CACHE_KEY, cache)


_LEADERBOARD_PERIOD_MAP = {"1d": "day", "7d": "week", "30d": "month", "all": "all"}


def fetch_leaderboard(period: str = "30d", limit: int = 20) -> list[str]:
    """Top wallets by PnL for the given period ('1d'|'7d'|'30d'|'all').

    MIGRATED TO DATA API V2 (2026-11-03). Polymarket retired v1
    (GET /v1/leaderboard — what this called before) on 2026-10-24 per
    their own migration docs, but it kept working here until it
    started returning 410 Gone sometime between 2026-11-02 20:15 and
    20:36 UTC — confirmed from the worker's own logs (last successful
    pull vs. the first "Got 0 candidate wallets" line). Because
    category_leaderboard._pull_merged_candidate_pool() wraps each
    fetch_leaderboard() call in its own try/except and just logs a
    warning on failure (by design — one bad period shouldn't kill a
    whole pass), this failed SILENTLY for roughly 41 hours straight:
    no exception surfaced, no crash, the worker just kept running full
    scan cycles pulling zero candidate wallets, so no Consensus/Early
    Mover/Elite Mover signal of any kind could fire — paper or live —
    the whole time. Nothing else in this codebase changed; this was
    purely Polymarket's API moving out from under it.

    v2 (GET /v2/leaderboard) differs in three ways that matter here:
      - the period parameter is renamed from 'period' to 'time_period',
        and its values change from '1d'/'7d'/'30d'/'all' to
        'day'/'week'/'month'/'all' (see _LEADERBOARD_PERIOD_MAP) — kept
        internal to this function so category_leaderboard.py's
        MERGE_PERIODS = ["30d", "7d", "1d"] and everything downstream
        of it needs no changes.
      - limit/offset pagination is replaced by an opaque
        pagination.next_cursor; v2 returns 400 if offset is sent at
        all (even 0), so this never sends one — the first request asks
        for `limit` directly, then follows next_cursor only if more
        are still needed.
      - the response is wrapped ({"data": [...], "pagination": {...}})
        with snake_case fields, and the wallet address field is
        renamed from entry["proxyWallet"] to entry["user_id"].
    sort_by='PNL' and category='overall' are passed explicitly to
    preserve this function's historical "global profit-ranked
    leaderboard" behavior — those happen to be v2's own defaults too,
    but being explicit means a future default change on Polymarket's
    end can't silently change what this pulls without this code
    changing too.
    """
    period_param = _LEADERBOARD_PERIOD_MAP.get(period, period)
    wallets: list[str] = []
    cursor = None
    while len(wallets) < limit:
        params = {"time_period": period_param, "sort_by": "PNL", "category": "overall"}
        if cursor:
            params["cursor"] = cursor
        else:
            params["limit"] = limit
        resp = requests.get(f"{DATA_API_BASE}/v2/leaderboard", params=params, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
        entries = payload.get("data") or []
        for entry in entries:
            wallet = entry.get("user_id")
            if wallet:
                wallets.append(wallet)
        cursor = (payload.get("pagination") or {}).get("next_cursor")
        if not cursor or not entries:
            break
    return wallets[:limit]


def fetch_positions(wallet: str) -> list[dict]:
    """Current open positions for a wallet. Returns raw dicts in the same
    v1-shaped keys consensus_logic.parse_positions() has always expected
    (conditionId, title, outcome, currentValue, size, curPrice, avgPrice,
    eventId, asset, endDate, eventSlug, slug, negativeRisk) — callers
    outside this module don't need to change.

    MIGRATED TO DATA API V2 (2026-11-03), same Polymarket v1 retirement as
    fetch_leaderboard() above (see that function's docstring for the full
    story). GET /positions is replaced by GET /v2/positions, which:
      - defaults to open positions only (same as this function's historical
        behavior — no status filter needed to match it)
      - wraps the response in {"data": [...], "pagination": {...}} instead
        of a bare array, with cursor-only pagination (no limit/offset on
        the old single-page-returns-everything assumption, so this now
        loops on next_cursor to keep returning ALL of a wallet's open
        positions, matching the old unlimited behavior)
      - renames fields to snake_case and in some cases restructures them;
        confirmed by hitting the live endpoint directly rather than
        guessing from docs (the OpenAPI spec's Position schema didn't
        render cleanly through available tooling): condition_id, title,
        outcome, current_value, current_size, current_price, avg_price,
        event_id, token_id, end_date, event_slug, slug, negative_risk.
    This function translates each v2 entry back into the legacy key names
    at the boundary — same pattern as fetch_leaderboard() — so
    consensus_logic.py and anything else downstream needs no changes.
    """
    positions: list[dict] = []
    cursor = None
    page_limit = 500
    while True:
        params = {"user": wallet, "limit": page_limit}
        if cursor:
            params["cursor"] = cursor
        resp = requests.get(f"{DATA_API_BASE}/v2/positions", params=params, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
        entries = payload.get("data") or []
        for e in entries:
            positions.append({
                "conditionId": e.get("condition_id"),
                "title": e.get("title"),
                "outcome": e.get("outcome"),
                "currentValue": e.get("current_value"),
                "size": e.get("current_value"),  # v1's stale-size fallback had no real v2
                                                   # equivalent worth trusting separately —
                                                   # current_value covers the same "don't
                                                   # crash if the primary field is absent"
                                                   # role parse_positions() already has
                "curPrice": e.get("current_price"),
                "avgPrice": e.get("avg_price"),
                "eventId": e.get("event_id"),
                "asset": e.get("token_id"),
                "endDate": e.get("end_date"),
                "eventSlug": e.get("event_slug"),
                "slug": e.get("slug"),
                "negativeRisk": e.get("negative_risk", False),
            })
        cursor = (payload.get("pagination") or {}).get("next_cursor")
        if not cursor or not entries:
            break
    return positions


def fetch_trades(wallet: str = None, market: str = None, limit: int = 500) -> list[dict]:
    """Historical fills, optionally filtered by wallet and/or market (conditionId).

    Used by the backtester to reconstruct what a wallet's position looked like
    at an arbitrary point in time, since /positions only gives current state.

    MIGRATED TO DATA API V2 (2026-11-03), same Polymarket v1 retirement as
    fetch_leaderboard()/fetch_positions() above. GET /trades becomes
    GET /v2/trades, the 'market' query param is renamed to 'condition' on
    the wire (this function's own 'market' parameter name is left
    unchanged so no caller needs to change), and the response moves to the
    {"data": [...], "pagination": {...}} envelope with cursor pagination,
    which this now follows until `limit` trades are collected or there's
    no next_cursor.

    UNVERIFIED FIELD NAMES: unlike fetch_leaderboard() and fetch_positions()
    above, the per-trade field names here have NOT been confirmed against a
    live v2 response or the backtester's own parsing code — the backtester
    isn't among the files available in this session, so its exact field
    expectations couldn't be checked. This function still returns each raw
    v2 trade dict as-is (not translated to legacy key names the way
    positions are above), since guessing a translation without seeing what
    actually reads it risked being wrong in a way that's harder to notice
    than an outright failure. If the backtester breaks on field names
    (v2 trade fields are snake_case, e.g. likely conditionId -> condition_id,
    makerAmount/takerAmount or similar -> snake_case equivalents), that's
    the next thing to fix — check its parsing against a live
    GET /v2/trades response before trusting backtest output again.
    """
    trades: list[dict] = []
    cursor = None
    while len(trades) < limit:
        params = {}
        if wallet:
            params["user"] = wallet
        if market:
            params["condition"] = market
        if cursor:
            params["cursor"] = cursor
        else:
            params["limit"] = limit
        resp = requests.get(f"{DATA_API_BASE}/v2/trades", params=params, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
        entries = payload.get("data") or []
        trades.extend(entries)
        cursor = (payload.get("pagination") or {}).get("next_cursor")
        if not cursor or not entries:
            break
    return trades[:limit]


def fetch_resolved_markets(limit: int = 100, offset: int = 0) -> list[dict]:
    """Closed/resolved markets with their final outcome, for backtesting.
    Explicitly sorted newest-first by ID (same proven proxy-for-recency
    pattern used for fetch_newest_events) — without this, results could
    be an arbitrary, possibly very old slice of Polymarket's ENTIRE
    history, easily falling outside any tracked wallet's visible trade
    window (fetch_trades is capped at 500 fills per wallet). Still not
    reliable for checking whether one SPECIFIC market has resolved (a
    resolved market has no guarantee of appearing in these first N
    results) — use check_market_resolution() below for that instead."""
    resp = requests.get(
        f"{GAMMA_API_BASE}/markets",
        params={"closed": "true", "order": "id", "ascending": "false", "limit": limit, "offset": offset},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def check_market_resolution(condition_id: str) -> str | None:
    """Check ONE specific market's resolution status directly via the CLOB
    API, by its condition ID — reliable regardless of how many other
    markets have resolved recently, since we're asking about this exact
    market rather than hoping it appears in a paginated "recently closed"
    list. Returns the winning outcome label if resolved, else None (still
    open, or the request failed — either way, "not confirmed resolved").
    """
    try:
        resp = requests.get(f"{CLOB_API_BASE}/markets/{condition_id}", timeout=15)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException:
        return None

    if not data.get("closed"):
        return None

    for token in data.get("tokens", []):
        if token.get("winner"):
            return token.get("outcome")
    return None


PREFERRED_CATEGORY_LABELS = [
    "Politics", "Sports", "Crypto", "Pop Culture", "Business",
    "Science", "Entertainment", "Elections", "Economy", "Weather",
]


def fetch_event_id_from_slug(slug: str) -> str:
    """/trades only returns an eventSlug (e.g. 'mlb-min-cws-2026-09-06'),
    never a numeric event ID — but fetch_event_categories needs a numeric
    ID, since it calls /events/{id}/tags. This resolves slug -> real ID
    via Gamma's dedicated single-event-by-slug endpoint. Returns "" if
    the slug can't be resolved (never guess an ID)."""
    try:
        resp = requests.get(f"{GAMMA_API_BASE}/events/slug/{slug}", timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException:
        return ""
    # Defensive: handle either a single event object or a list containing one.
    if isinstance(data, list):
        data = data[0] if data else {}
    return str(data.get("id") or "")


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


def fetch_newest_events(limit: int = 100) -> list[dict]:
    """The newest active events on Polymarket, sorted newest-first, each
    including its markets (with conditionId) — used to detect brand-new
    markets that appeared since the last check."""
    resp = requests.get(
        f"{GAMMA_API_BASE}/events",
        params={"order": "id", "ascending": "false", "active": "true", "closed": "false", "limit": limit},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def polite_sleep(seconds: float = 0.2):
    time.sleep(seconds)
