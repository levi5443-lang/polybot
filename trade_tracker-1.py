"""
trade_tracker.py — resolves open trades against real outcomes and computes
accuracy. Works for both paper trades (mode='paper', no money moved) and
live trades (mode='live', real money) — same ledger (risk_manager's
trade_log.json), same resolution logic.

Since real execution isn't live yet, this is what lets you build an actual
track record from paper trades BEFORE ever risking money — you'll have
real win-rate numbers to look at, not just a hope that the strategy works.
"""

import json
import os
import logging

from polymarket_api import check_market_resolution, polite_sleep
from risk_manager import _load_trade_log, _save_trade_log
from datetime import datetime, timezone

log = logging.getLogger("trade_tracker")

from storage_paths import persistent_path

DIGEST_STATE_FILE = persistent_path("digest_state.json")


def sync_resolved_trades() -> int:
    """Check every 'open' ledger entry against ITS OWN specific market,
    directly — not against a paginated list of "recently closed" markets
    (that approach silently missed real resolutions, since a resolved
    market has no guarantee of appearing in the first N results of an
    unordered list). For each one whose market has resolved, mark it won
    or lost based on whether our recorded outcome matches the winner.

    Returns how many trades got resolved this pass.
    """
    trades = _load_trade_log()
    open_trades = [t for t in trades if t["status"] == "open"]
    if not open_trades:
        return 0

    # Multiple ledger entries can share the same market_id (e.g. two
    # different wallets' predictions on the same market) — only check each
    # distinct market once per pass.
    unique_market_ids = list(dict.fromkeys(t["market_id"] for t in open_trades))
    resolution_by_market: dict[str, str] = {}
    for market_id in unique_market_ids:
        outcome = check_market_resolution(market_id)
        if outcome:
            resolution_by_market[market_id] = outcome
        polite_sleep(0.1)

    resolved_count = 0
    for t in open_trades:
        actual_outcome = resolution_by_market.get(t["market_id"])
        if actual_outcome is None:
            continue  # still open, or the check failed — try again next cycle

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


def get_resolved_trades_today(mode: str = None) -> list[dict]:
    """Individual trade records closed today (UTC calendar day), most
    recent first — this is what lets the digest actually SHOW which
    specific trades won or lost, not just an aggregate percentage."""
    trades = _load_trade_log()
    today = datetime.now(timezone.utc).date()
    result = []
    for t in trades:
        if t["status"] != "closed" or "correct" not in t or not t.get("closed_at"):
            continue
        if mode and t.get("mode") != mode:
            continue
        try:
            closed_date = datetime.fromisoformat(t["closed_at"]).date()
        except (ValueError, TypeError):
            continue
        if closed_date == today:
            result.append(t)
    result.sort(key=lambda t: t["closed_at"], reverse=True)
    return result


def format_accuracy_summary(mode: str = "paper") -> str:
    stats = get_accuracy(mode=mode)
    if stats["total_closed"] == 0:
        return f"[{mode}] No resolved trades yet — nothing to measure accuracy from."
    return (f"[{mode}] Accuracy: {stats['wins']}/{stats['total_closed']} correct "
            f"({stats['win_rate_pct']}%)")


MAX_TODAY_LINES = 15  # keeps the digest well under Telegram's message length limit


def _format_trade_line(t: dict) -> str:
    emoji = "✅" if t.get("correct") else "❌"
    mode_tag = "" if t.get("mode") == "paper" else " (LIVE)"
    return (f"{emoji} [{t.get('category', 'Uncategorized')}]{mode_tag} {t['market_question']} — "
            f"predicted {t['outcome']}, actual {t.get('actual_outcome', '?')}")


MAX_HISTORY_LINES = 15  # same length safety cap, applied to the on-demand /history command


def get_recent_resolved_trades(n: int = MAX_HISTORY_LINES) -> list[dict]:
    """Most recent resolved trades overall (any day), most recent first —
    what /history on Telegram shows, as opposed to the daily digest which
    only covers today."""
    trades = _load_trade_log()
    closed = [t for t in trades if t["status"] == "closed" and "correct" in t and t.get("closed_at")]
    closed.sort(key=lambda t: t["closed_at"], reverse=True)
    return closed[:n]


def format_history_message(n: int = MAX_HISTORY_LINES) -> str:
    """Response for the /history Telegram command."""
    recent = get_recent_resolved_trades(n)
    if not recent:
        return "📜 *Trade History*\n\nNo resolved trades yet."
    lines = [f"📜 *Trade History* (last {len(recent)})\n"]
    for t in recent:
        lines.append(_format_trade_line(t))
    return "\n".join(lines)


def format_accuracy_command_message() -> str:
    """Response for the /accuracy Telegram command — same numbers as the
    daily digest's running totals, available on demand."""
    paper = get_accuracy(mode="paper")
    live = get_accuracy(mode="live")
    lines = ["📊 *Accuracy*\n"]
    if paper["total_closed"] > 0:
        lines.append(f"Paper: {paper['wins']}/{paper['total_closed']} correct "
                      f"({paper['win_rate_pct']}%)")
    else:
        lines.append("Paper: no resolved trades yet")
    if live["total_closed"] > 0:
        lines.append(f"Live: {live['wins']}/{live['total_closed']} correct "
                      f"({live['win_rate_pct']}%)")
    else:
        lines.append("Live: no resolved trades yet")
    trades = _load_trade_log()
    open_count = sum(1 for t in trades if t["status"] == "open")
    lines.append(f"\nCurrently open: {open_count} trade(s) awaiting resolution")
    return "\n".join(lines)


def format_daily_digest_message() -> str:
    """A single Telegram-ready message: today's individual trade results
    (win/loss, one line each) PLUS running totals across everything ever
    tracked. Today's list is capped at MAX_TODAY_LINES to stay well under
    Telegram's message length limit — the running totals still reflect
    everything regardless of the cap."""
    paper = get_accuracy(mode="paper")
    live = get_accuracy(mode="live")

    today_trades = get_resolved_trades_today()  # both modes, most recent first

    lines = ["📊 *Daily Accuracy Digest*\n"]

    lines.append("*Today's results:*")
    if not today_trades:
        lines.append("No trades resolved today.")
    else:
        shown = today_trades[:MAX_TODAY_LINES]
        for t in shown:
            lines.append(_format_trade_line(t))
        remaining = len(today_trades) - len(shown)
        if remaining > 0:
            lines.append(f"...and {remaining} more today (see full history in the trade log).")

    lines.append("")
    lines.append("*Running totals:*")
    if paper["total_closed"] > 0:
        lines.append(f"Paper: {paper['wins']}/{paper['total_closed']} correct "
                      f"({paper['win_rate_pct']}%)")
    else:
        lines.append("Paper: no resolved trades yet")

    if live["total_closed"] > 0:
        lines.append(f"Live: {live['wins']}/{live['total_closed']} correct "
                      f"({live['win_rate_pct']}%)")
    else:
        lines.append("Live: no resolved trades yet")

    trades = _load_trade_log()
    open_count = sum(1 for t in trades if t["status"] == "open")
    lines.append(f"\nCurrently open: {open_count} trade(s) awaiting resolution")

    return "\n".join(lines)


def _load_digest_state() -> dict:
    if os.path.exists(DIGEST_STATE_FILE):
        try:
            with open(DIGEST_STATE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_digest_state(state: dict) -> None:
    try:
        with open(DIGEST_STATE_FILE, "w") as f:
            json.dump(state, f)
    except OSError as e:
        log.error("Could not save digest state: %s", e)


def maybe_send_daily_digest(send_fn) -> bool:
    """Sends the daily digest at most once per UTC calendar day, regardless
    of how many poll cycles run in that day. send_fn is a callable taking
    the message string (e.g. telegram_alert.send_telegram_alert) — passed
    in rather than imported directly so this stays easy to test.

    Returns True if a digest was actually sent this call.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    state = _load_digest_state()

    if state.get("last_sent_date") == today:
        return False  # already sent today

    message = format_daily_digest_message()
    send_fn(message)
    state["last_sent_date"] = today
    _save_digest_state(state)
    log.info("Daily digest sent.")
    return True
