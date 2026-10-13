"""
polybot_client.py — thin client for the Polybot Account Trading API.

This is the ONLY place in the codebase that talks to Polybot
(developer-api.prdctlbz.dev). It wraps:

  - GET  /api/v1/account/balance
  - GET  /api/v1/account/positions
  - GET  /api/v1/account/orders
  - GET  /api/v1/account/orders/requests/{requestId}
  - POST /api/v1/account/orders/market-buy
  - POST /api/v1/account/orders/market-sell
  - POST /api/v1/account/orders/limit-buy
  - POST /api/v1/account/orders/limit-sell
  - DELETE /api/v1/account/orders/{orderId}

Auth: API key in the `x-api-key` header (NOT Bearer). Configured via two
env vars, read once at import time:

  POLYBOT_API_BASE_URL — e.g. https://developer-api.prdctlbz.dev/api/v1/account
  POLYBOT_API_KEY       — the account's scoped key (read/trade/cancel)

If either is unset, every function here raises PolybotNotConfigured rather
than silently no-op'ing — callers (execute_trade/request_live_trade in
consensus_bot.py) must handle that explicitly, not assume success.

LIVE EXECUTION IS SEPARATELY GATED. This module will happily place a real
order if asked to — but nothing in this codebase calls the order-placement
functions unless live_execution.is_enabled() (see live_execution.py)
returns True. That flag defaults to OFF and only Levi flips it, by design
(he's explicit that paper trading gets perfected first, live funding comes
later). Do not wire a call to place_market_buy/place_market_sell/
place_limit_buy/place_limit_sell anywhere that isn't already behind that
check.
"""

import logging
import os
import uuid

import requests

log = logging.getLogger("polybot_client")

BASE_URL = os.environ.get("POLYBOT_API_BASE_URL", "").rstrip("/")
API_KEY = os.environ.get("POLYBOT_API_KEY", "")

REQUEST_TIMEOUT_SECONDS = 20


class PolybotError(Exception):
    """Raised for any non-2xx response from Polybot, or a malformed one."""
    def __init__(self, message, status_code=None, error_code=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.body = body


class PolybotNotConfigured(Exception):
    """Raised when POLYBOT_API_BASE_URL / POLYBOT_API_KEY aren't set."""


def _require_config():
    if not BASE_URL or not API_KEY:
        raise PolybotNotConfigured(
            "POLYBOT_API_BASE_URL and/or POLYBOT_API_KEY are not set — "
            "Polybot integration is not configured."
        )


def _headers():
    return {"x-api-key": API_KEY, "Content-Type": "application/json"}


def _get(path, params=None):
    _require_config()
    url = f"{BASE_URL}{path}"
    try:
        resp = requests.get(url, headers=_headers(), params=params, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        raise PolybotError(f"Request to {path} failed: {e}") from e
    return _handle_response(resp, path)


def _post(path, body):
    _require_config()
    url = f"{BASE_URL}{path}"
    try:
        resp = requests.post(url, headers=_headers(), json=body, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        raise PolybotError(f"Request to {path} failed: {e}") from e
    return _handle_response(resp, path)


def _delete(path):
    _require_config()
    url = f"{BASE_URL}{path}"
    try:
        resp = requests.delete(url, headers=_headers(), timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        raise PolybotError(f"Request to {path} failed: {e}") from e
    return _handle_response(resp, path)


def _handle_response(resp, path):
    try:
        data = resp.json() if resp.content else {}
    except ValueError:
        data = {}

    if resp.status_code >= 400:
        error_code = data.get("errorCode") if isinstance(data, dict) else None
        raise PolybotError(
            f"Polybot {path} returned HTTP {resp.status_code} (errorCode={error_code}): {data}",
            status_code=resp.status_code, error_code=error_code, body=data,
        )
    return data


# ---------------------------------------------------------------------------
# Read endpoints (read scope)
# ---------------------------------------------------------------------------

def get_balance() -> dict:
    """Returns {tradableBalance, positionsValue, reservedUsdTotal,
    allocatedUsdTotal} — all strings per the spec, caller converts."""
    return _get("/balance")


def get_positions(limit: int = 20, offset: int = 0) -> dict:
    """Returns {positions: [...], hasMore}. limit capped at 100 by the API."""
    return _get("/positions", params={"limit": limit, "offset": offset})


def get_orders(limit: int = 20, offset: int = 0) -> dict:
    """Returns {orders: [...], totalCount, hasMore}."""
    return _get("/orders", params={"limit": limit, "offset": offset})


def get_request_status(request_id: str) -> dict:
    """Returns {kind, status, orderId, terminal, ...timestamps} for a
    previously-submitted requestId — use this to poll an order placed with
    state 'pending'/'in_flight'/'submitted' until it reaches a terminal
    state."""
    return _get(f"/orders/requests/{request_id}")


# ---------------------------------------------------------------------------
# Cancel (cancel scope)
# ---------------------------------------------------------------------------

def cancel_order(order_id: str) -> dict:
    """Returns {orderId, success}."""
    return _delete(f"/orders/{order_id}")


# ---------------------------------------------------------------------------
# Order placement (trade scope) — REAL MONEY once live_execution is on.
# ---------------------------------------------------------------------------

def place_market_buy(token_id, market_slug, outcome, negative_risk, amount_usd,
                      expected_price, max_price, request_id=None,
                      price_impact_confirmation_token=None) -> dict:
    """amount_usd is USD to spend, not shares. request_id defaults to a
    fresh UUID if not given — pass the SAME request_id back in on a retry
    of this exact order (idempotency); never retry an uncertain submission
    with a new ID (spec's own wording)."""
    body = {
        "requestId": request_id or str(uuid.uuid4()),
        "tokenId": token_id,
        "marketSlug": market_slug,
        "outcome": outcome,
        "negativeRisk": negative_risk,
        "amount": amount_usd,
        "expectedPrice": expected_price,
        "maxPrice": max_price,
    }
    if price_impact_confirmation_token:
        body["priceImpactConfirmationToken"] = price_impact_confirmation_token
    return _post("/orders/market-buy", body)


def place_market_sell(token_id, market_slug, outcome, negative_risk, shares,
                       request_id=None) -> dict:
    body = {
        "requestId": request_id or str(uuid.uuid4()),
        "tokenId": token_id,
        "marketSlug": market_slug,
        "outcome": outcome,
        "negativeRisk": negative_risk,
        "shares": shares,
    }
    return _post("/orders/market-sell", body)


def place_limit_buy(token_id, market_slug, outcome, negative_risk, shares, price,
                     request_id=None) -> dict:
    """Limit orders are good-till-cancelled per the spec — call
    cancel_order() explicitly by orderId to pull one back; it does not
    expire on its own."""
    body = {
        "requestId": request_id or str(uuid.uuid4()),
        "tokenId": token_id,
        "marketSlug": market_slug,
        "outcome": outcome,
        "negativeRisk": negative_risk,
        "shares": shares,
        "price": price,
    }
    return _post("/orders/limit-buy", body)


def place_limit_sell(token_id, market_slug, outcome, negative_risk, shares, price,
                      request_id=None) -> dict:
    body = {
        "requestId": request_id or str(uuid.uuid4()),
        "tokenId": token_id,
        "marketSlug": market_slug,
        "outcome": outcome,
        "negativeRisk": negative_risk,
        "shares": shares,
        "price": price,
    }
    return _post("/orders/limit-sell", body)
