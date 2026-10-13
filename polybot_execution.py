"""
polybot_execution.py — live execution backend for trade_approval.py,
backed by the Polybot custodial account API (polybot_client.py) rather
than the self-custodied, direct-CLOB path in execution.py.

BACKGROUND (2026-10-13, Levi's request): execution.py already existed —
direct order placement against Polymarket's CLOB, signed by
POLYMARKET_PRIVATE_KEY — but per risk_manager.py's own docstring it was
never actually exercised against a funded live wallet. Levi separately
obtained a scoped Polybot account key (read/trade/cancel, no withdrawal)
specifically so the bot can execute live trades on its own, with paper
trading perfected first and the live account funded later. This module
is the new backend that trade_approval.py's _execute_approved_trade()
uses instead of execution.py — same function shapes
(get_wallet_balance_usd / get_wallet_status / place_market_buy), so the
approval flow itself barely changes.

execution.py is left completely untouched and unused by default. Nothing
here deletes or disables it — switching back would just mean changing
trade_approval.py's import again.

GATING: place_market_buy() below refuses outright unless
live_execution.is_enabled() is True (see live_execution.py). That flag
is the single kill switch; this module does not duplicate or
second-guess it anywhere else.
"""

import logging

import live_execution
import polybot_client

log = logging.getLogger("polybot_execution")

# How far above expected_price Polybot is allowed to actually fill a
# market buy before we'd rather the order get rejected than fill badly.
# This is a pure execution-safety knob (protecting against a bad/slipped
# fill), NOT one of Levi's position-sizing or exposure rules — those are
# confirmed (2% of tradable cash per trade, 26% total exposure of
# tradable cash) and this doesn't touch them. Flagging the default here
# rather than burying it: 3 cents or 3% of expected_price, whichever is
# SMALLER (tighter near the edges of the price range, where 3% is a
# bigger absolute move), capped so maxPrice never exceeds $0.99. Tell me
# if you want this tighter/looser.
MAX_SLIPPAGE_ABS = 0.03
MAX_SLIPPAGE_PCT = 0.03
MAX_PRICE_CEILING = 0.99


def _default_max_price(expected_price: float) -> float:
    slippage = min(MAX_SLIPPAGE_ABS, expected_price * MAX_SLIPPAGE_PCT)
    return round(min(expected_price + slippage, MAX_PRICE_CEILING), 4)


def get_wallet_balance_usd() -> float:
    """Returns Polybot's tradableBalance — the figure Levi confirmed both
    the 2%-per-trade and 26%-total-exposure caps are measured against
    (cash on hand, NOT total account equity including open positions)."""
    data = polybot_client.get_balance()
    return float(data["tradableBalance"])


def get_live_committed_usd() -> float:
    """Real, authoritative exposure right now per Polybot itself: open
    positions' value plus anything reserved against pending orders. Used
    INSTEAD of (not in addition to) the local trade ledger for the
    exposure-cap check, because the local ledger only knows about trades
    this bot itself placed — it has no visibility into positions that
    existed in the Polybot account before this integration (the owner
    mentioned ~$62.82 already in positions) or anything placed outside
    the bot. Polybot's own numbers are ground truth for what's actually
    committed."""
    data = polybot_client.get_balance()
    return float(data["positionsValue"]) + float(data["reservedUsdTotal"])


def get_wallet_status() -> dict:
    """Shape-compatible with execution.py's get_wallet_status() so
    trade_approval.py's /wallet command doesn't need its own branch.
    There's no on-chain address here — the account is identified by the
    API key alone — reported plainly rather than faking an address."""
    try:
        balance = polybot_client.get_balance()
    except polybot_client.PolybotNotConfigured as e:
        return {"mode": "error", "error": str(e)}
    except polybot_client.PolybotError as e:
        return {"mode": "error", "error": str(e)}
    return {
        "mode": "polybot",
        "trading_address": "Polybot account (API-key based, no on-chain address)",
        "tradable_balance": float(balance["tradableBalance"]),
        "positions_value": float(balance["positionsValue"]),
    }


def place_market_buy(token_id: str, size_usd: float, market_slug: str = "",
                      outcome: str = "", negative_risk: bool = False,
                      expected_price: float = None, max_price: float = None) -> dict:
    """Places a REAL market-buy order through Polybot. Refuses outright
    unless live_execution.is_enabled() — this is the one place that
    check happens for order placement; callers should not duplicate it,
    just let this raise.

    token_id/market_slug/outcome/negative_risk must all describe the
    SAME market outcome — Polybot validates this itself, but a mismatch
    here means something upstream (consensus_logic.parse_positions)
    didn't populate these fields correctly, which is worth knowing about
    loudly rather than having Polybot quietly reject it.

    expected_price is required (there's no sane default — it has to come
    from the signal's actual current price). max_price defaults to a
    small slippage allowance above expected_price if not given — see
    _default_max_price above.
    """
    if not live_execution.is_enabled():
        raise RuntimeError(
            "POLYBOT_LIVE_EXECUTION_ENABLED is not set to true — refusing to place "
            "a real order. This is the deliberate kill switch; it only gets flipped "
            "on Levi's explicit instruction."
        )

    if expected_price is None:
        raise ValueError("expected_price is required to place a live Polybot market buy.")
    if not market_slug:
        raise ValueError(
            "market_slug is empty — refusing to place an order without it. This "
            "usually means consensus_logic.parse_positions() didn't find a 'slug' "
            "field on the raw position data; check that before retrying."
        )

    if max_price is None:
        max_price = _default_max_price(expected_price)

    resp = polybot_client.place_market_buy(
        token_id=token_id, market_slug=market_slug, outcome=outcome,
        negative_risk=negative_risk, amount_usd=size_usd,
        expected_price=expected_price, max_price=max_price,
    )

    # A non-terminal state (submitted/pending/in_flight) is NOT proof of a
    # fill, and per Polybot's own guidance an uncertain result must be
    # reconciled by polling get_request_status with the SAME requestId —
    # never retried with a new one. Surface that clearly here rather than
    # letting the caller assume success just because this didn't raise.
    state = resp.get("state")
    if state == "rejected":
        raise RuntimeError(f"Order rejected by Polybot: {resp.get('errorCode')} — {resp}")
    if state in ("unknown", "in_flight", "pending", "submitted"):
        log.warning(
            "Order state is '%s' after submission (requestId=%s) — not a confirmed "
            "fill yet. Poll polybot_client.get_request_status() with this same "
            "requestId before assuming anything about this trade's outcome.",
            state, resp.get("requestId"),
        )
    if resp.get("errorCode") == "PRICE_IMPACT_CONFIRMATION_REQUIRED":
        log.warning(
            "Price impact confirmation required (requestId=%s) — this order has "
            "NOT been submitted. A confirmation token would need to be reviewed "
            "and passed back explicitly; this bot does not auto-confirm price-impact "
            "challenges.", resp.get("requestId"),
        )

    return resp
