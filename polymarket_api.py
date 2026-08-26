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


def fetch_leaderboard(window: str = "month", limit: int = 20) -> list[str]:
    """Top wallets by PnL/volume for the given window ('day'|'week'|'month'|'all')."""
    resp = requests.get(
        f"{DATA_API_BASE}/leaderboard", params={"window": window, "limit": limit}, timeout=15
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


def polite_sleep(seconds: float = 0.2):
    time.sleep(seconds)
