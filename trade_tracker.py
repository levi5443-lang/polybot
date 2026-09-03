"""
trade_tracker.py — resolves open trades against real outcomes and computes
accuracy. Works for both paper trades (mode='paper', no money moved) and
live trades (mode='live', real money) — same ledger (risk_manager's
trade_log.json), same resolution logic.

Since real execution isn't live yet, this is what lets you build an actual
track record from paper trades BEFORE ever risking money — you'll have
real win-rate numbers to look at, not just a hope that the strategy works.
"""

import logging

from polymarket_api import fetch_resolved_markets
from risk_manager import _load_trade_log, _save_trade_log
from datetime import datetime, timezone

log = logging.getLogger("trade_tracker")


def sync_resolved_trades(lookback_markets: int = 200) -> int:
    """Check all 'open' ledger entries against recently resolved markets.
    For each one whose market has resolved, mark it won or lost based on
    whether our recorded outcome matches the actual winning outcome.

    Returns how many trades got resolved this pass.
    """
    trades = _load_trade_log()
    open_trades = [t for t in trades if t["status"] == "open"]
    if not open_trades:
        return 0

    try:
        resolved_markets = fetch_resolved_markets(limit=lookback_markets)
    except Exception as e:
        log.warning("Could not fetch resolved markets for accuracy sync: %s", e)
        return 0

    # Map conditionId -> actual winning outcome, for fast lookup
    resolved_by_market: dict[str, str] = {}
    for m in resolved_markets:
        cid = m.get("conditionId") or m.get("id")
        winning_outcome = m.get("resolvedOutcome") or m.get("outcome")
        if cid and winning_outcome:
            resolved_by_market[str(cid)] = str(winning_outcome)

    resolved_count = 0
    for t in open_trades:
        actual_outcome = resolved_by_market.get(t["market_id"])
        if actual_outcome is None:
            continue  # not resolved yet, or not in this lookback window

        correct = (t["outcome"] == actual_outcome)

        if t.get("mode") == "paper":
            # Paper trades don't move money — "P&L" is just a correctness
            # marker: +1 for correct, -1 for incorrect, so existing win/loss
            # math (positive=win, negative=loss) keeps working unchanged.
            pnl = 1.0 if correct else -1.0
        else:
            # Live trades: a real P&L number needs actual entry/exit prices,
            # which isn't wired up yet — approximate with the full stake
            # won/lost until real fill-price tracking is added.
            pnl = t["size_usd"] if correct else -t["size_usd"]

        t["status"] = "closed"
        t["realized_pnl"] = pnl
        t["closed_at"] = datetime.now(timezone.utc).isoformat()
        t["actual_outcome"] = actual_outcome
        t["correct"] = correct
        resolved_count += 1
        log.info("Resolved: '%s' [predicted %s, actual %s] -> %s",
                  t["market_question"], t["outcome"], actual_outcome,
                  "CORRECT" if correct else "WRONG")

    if resolved_count:
        _save_trade_log(trades)

    return resolved_count


def get_accuracy(mode: str = None, category: str = None) -> dict:
    """Win rate across closed trades, optionally filtered by mode
    ('paper'/'live') and/or category. Returns a dict with counts and
    win_rate_pct (None if there's nothing closed yet to compute from)."""
    trades = _load_trade_log()
    closed = [t for t in trades if t["status"] == "closed" and "correct" in t]

    if mode:
        closed = [t for t in closed if t.get("mode") == mode]
    if category:
        closed = [t for t in closed if t.get("category") == category]

    wins = sum(1 for t in closed if t["correct"])
    losses = len(closed) - wins
    total = len(closed)
    win_rate = round(100 * wins / total, 1) if total > 0 else None

    return {
        "total_closed": total,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": win_rate,
    }


def format_accuracy_summary(mode: str = "paper") -> str:
    stats = get_accuracy(mode=mode)
    if stats["total_closed"] == 0:
        return f"[{mode}] No resolved trades yet — nothing to measure accuracy from."
    return (f"[{mode}] Accuracy: {stats['wins']}/{stats['total_closed']} correct "
            f"({stats['win_rate_pct']}%)")
