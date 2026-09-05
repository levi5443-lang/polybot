"""
risk_manager.py — position sizing and daily loss cap for live execution.

This is the safety layer that sits BETWEEN a consensus signal and an actual
order. It answers two questions every time execute_trade() considers
placing a real order:

  1. How much should this trade be sized at? (POSITION_SIZE_PCT of current
     wallet balance)
  2. Are we allowed to trade at all right now? (has today's realized loss
     already hit DAILY_LOSS_CAP_USD?)

Trades this bot places are recorded to a local JSON ledger (TRADE_LOG_FILE)
so the daily loss check has something to work from — Polymarket's own API
doesn't give a clean "today's realized P&L" figure, so we track our own.

IMPORTANT: this module's math is fully unit-testable and IS tested. What is
NOT tested (because it can't be, without a funded live wallet) is whether
the actual order placement in execution.py behaves as documented against
Polymarket's real CLOB. Treat that part as an unverified first draft.
"""

import logging
from datetime import datetime, timezone

log = logging.getLogger("risk_manager")

POSITION_SIZE_PCT = 0.02      # 2% of wallet balance per trade
DAILY_LOSS_CAP_USD = 100.0    # stop opening new trades once today's realized losses hit this
TOTAL_EXPOSURE_CAP_PCT = 0.26 # never have more than this fraction of the wallet across ALL open live positions combined

# Shared across both services (worker + dashboard) — this is what makes
# /history and /accuracy on Telegram (served by the dashboard) able to see
# trades the worker actually placed. A local-only file would be invisible
# across that service boundary.
from shared_storage import get_json, set_json

TRADE_LOG_KEY = "trade_log.json"


def _load_trade_log() -> list[dict]:
    return get_json(TRADE_LOG_KEY, [])


def _save_trade_log(trades: list[dict]) -> None:
    set_json(TRADE_LOG_KEY, trades)


def get_position_size_usd(wallet_balance_usd: float) -> float:
    """2% of current wallet balance. Recomputed fresh each trade — NOT 2%
    of some fixed starting amount — so it naturally shrinks if the bankroll
    is down and grows if it's up."""
    return round(wallet_balance_usd * POSITION_SIZE_PCT, 2)


def get_current_live_exposure_usd() -> float:
    """Sum of size_usd across every OPEN, live-mode trade — how much is
    currently at risk across all live positions combined, right now."""
    trades = _load_trade_log()
    return sum(t["size_usd"] for t in trades if t.get("mode") == "live" and t["status"] == "open")


def compute_trade_size_usd(wallet_balance_usd: float) -> float:
    """The size a new LIVE trade should use: 2% of current balance, but
    never allowed to push total open live exposure past
    TOTAL_EXPOSURE_CAP_PCT of the wallet.

    Returns 0.0 if the 2% trade wouldn't fit under the cap at all — trades
    are SKIPPED entirely in that case, never silently downsized, so every
    live position stays a consistent, predictable 2% risk rather than some
    smaller leftover amount that's harder to reason about.
    """
    proposed = get_position_size_usd(wallet_balance_usd)
    current_exposure = get_current_live_exposure_usd()
    cap_usd = TOTAL_EXPOSURE_CAP_PCT * wallet_balance_usd

    if current_exposure + proposed > cap_usd:
        log.info("Trade size blocked by exposure cap: current $%.2f + proposed $%.2f "
                  "would exceed %.0f%% cap ($%.2f of $%.2f balance).",
                  current_exposure, proposed, TOTAL_EXPOSURE_CAP_PCT * 100, cap_usd, wallet_balance_usd)
        return 0.0

    return proposed


def has_open_trade(market_id: str, outcome: str, mode: str = "live") -> bool:
    """True if there's already an open ledger entry for this exact
    market+outcome+mode. Prevents re-trading (or re-logging a paper trade
    for) the same ongoing signal every single poll cycle."""
    trades = _load_trade_log()
    return any(
        t["market_id"] == market_id and t["outcome"] == outcome
        and t.get("mode", "live") == mode and t["status"] == "open"
        for t in trades
    )


def record_trade_open(market_id: str, market_question: str, outcome: str,
                       size_usd: float, entry_price: float, category: str,
                       mode: str = "live", signal_type: str = "consensus",
                       end_date: str = "") -> None:
    """mode is 'live' (real money) or 'paper' (tracked for accuracy only,
    no money moved). signal_type is 'consensus' or 'early_mover' — lets
    accuracy be broken down by which kind of signal produced the trade.
    end_date is Polymarket's own expected-resolution date for the market,
    carried through so /positions can show when a decision is expected."""
    trades = _load_trade_log()
    trades.append({
        "market_id": market_id,
        "market_question": market_question,
        "outcome": outcome,
        "category": category,
        "size_usd": size_usd,
        "entry_price": entry_price,
        "mode": mode,
        "signal_type": signal_type,
        "end_date": end_date,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "status": "open",
        "realized_pnl": None,
        "closed_at": None,
    })
    _save_trade_log(trades)
    log.info("Recorded %s trade open: %s [%s] $%.2f @ %.3f",
              mode, market_question, outcome, size_usd, entry_price)


def record_trade_closed(market_id: str, outcome: str, realized_pnl: float) -> None:
    """Call this once a position resolves (win or loss) to close out its
    ledger entry so the daily loss cap can account for it."""
    trades = _load_trade_log()
    for t in trades:
        if t["market_id"] == market_id and t["outcome"] == outcome and t["status"] == "open":
            t["status"] = "closed"
            t["realized_pnl"] = realized_pnl
            t["closed_at"] = datetime.now(timezone.utc).isoformat()
    _save_trade_log(trades)


def todays_realized_loss_usd() -> float:
    """Sum of realized LIVE losses (negative P&L only) on trades closed
    today (UTC calendar day). Paper trades never count toward this — the
    cap is about real money, not tracked accuracy. Wins don't offset this
    either — the cap limits how much can be LOST in a day, not net P&L."""
    trades = _load_trade_log()
    today = datetime.now(timezone.utc).date()
    total_loss = 0.0
    for t in trades:
        if t.get("mode", "live") != "live":
            continue
        if t["status"] != "closed" or t["closed_at"] is None:
            continue
        closed_date = datetime.fromisoformat(t["closed_at"]).date()
        if closed_date != today:
            continue
        pnl = t.get("realized_pnl") or 0
        if pnl < 0:
            total_loss += abs(pnl)
    return total_loss


def daily_loss_cap_reached() -> bool:
    loss = todays_realized_loss_usd()
    reached = loss >= DAILY_LOSS_CAP_USD
    if reached:
        log.warning("Daily loss cap reached: $%.2f realized loss today (cap=$%.2f). Blocking new trades.",
                     loss, DAILY_LOSS_CAP_USD)
    return reached
