"""
trade_approval.py — approve-before-you-trade flow for LIVE (real money)
trades, entirely within the worker process.

This deliberately does NOT touch the dashboard/webhook at all. The
worker polls Telegram for your button tap itself (same getUpdates
mechanism the webhook uses, just from inside this process instead) —
which means your private key, loaded only into this worker via
POLYMARKET_PRIVATE_KEY, never needs to exist anywhere near a
publicly-reachable service. There is a real tradeoff for that safety:
your tap won't execute instantly — it'll be picked up on the worker's
next cycle, so up to POLL_INTERVAL_SECONDS (5 minutes) after you tap.

Flow:
  1. A live consensus signal fires -> request_trade_approval() sends a
     Telegram message with Approve/Reject buttons and stores a pending
     record (shared storage, survives restarts).
  2. Every cycle, process_pending_approvals() polls Telegram for new
     button taps. On Approve: re-runs every risk check fresh (dedup,
     daily loss cap, 2%-of-balance sizing capped by the 26% total
     exposure rule) and places the order if everything still checks out.
     On Reject: just drops the pending request.
  3. Anything left unanswered past APPROVAL_TIMEOUT_MINUTES silently
     expires — a signal from an hour ago may no longer reflect current
     positioning, so we don't execute stale approvals.

Paper trades never go through this — they still auto-log immediately via
execute_trade() in consensus_bot.py, since no real money is at risk.
"""

import logging
import uuid
from datetime import datetime, timezone

from shared_storage import get_json, set_json
from telegram_alert import (
    send_telegram_message_with_buttons, get_telegram_updates,
    answer_callback_query, send_telegram_alert,
)
import risk_manager
import wallet_tracker

log = logging.getLogger("trade_approval")

PENDING_APPROVALS_KEY = "pending_approvals.json"
LAST_UPDATE_ID_KEY = "telegram_last_update_id.json"
APPROVAL_TIMEOUT_MINUTES = 60


def _load_pending() -> dict:
    return get_json(PENDING_APPROVALS_KEY, {})


def _save_pending(pending: dict) -> None:
    set_json(PENDING_APPROVALS_KEY, pending)


def _load_last_update_id() -> int:
    state = get_json(LAST_UPDATE_ID_KEY, {})
    return state.get("last_update_id", 0)


def _save_last_update_id(update_id: int) -> None:
    set_json(LAST_UPDATE_ID_KEY, {"last_update_id": update_id})


def has_pending_approval(market_id: str, outcome: str) -> bool:
    """Used by consensus_bot.py to avoid asking for approval again on
    every cycle a signal keeps re-appearing."""
    pending = _load_pending()
    return any(
        p["market_id"] == market_id and p["outcome"] == outcome
        for p in pending.values()
    )


def request_trade_approval(signal) -> None:
    """Send an Approve/Reject prompt for a live signal and remember it."""
    short_id = uuid.uuid4().hex[:8]
    pending = _load_pending()
    pending[short_id] = {
        "market_id": signal.market_id,
        "outcome": signal.outcome,
        "market_question": signal.market_question,
        "category": signal.category,
        "token_id": signal.token_id,
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_pending(pending)

    record_str = wallet_tracker.format_wallet_record(
        signal.agreeing_wallets[0], signal.category
    ) if signal.agreeing_wallets else None
    record_line = f"\n{signal.category} record (lead trader): {record_str}" if record_str else ""

    message = (
        f"🔔 *APPROVAL NEEDED*  _[{signal.category}]_\n\n"
        f"Market: {signal.market_question}\n"
        f"Side: *{signal.outcome}*\n"
        f"Agreement: {signal.count} tracked top traders{record_line}\n\n"
        f"Take this trade?"
    )
    buttons = [[
        {"text": "✅ Approve", "callback_data": f"approve:{short_id}"},
        {"text": "❌ Reject", "callback_data": f"reject:{short_id}"},
    ]]
    send_telegram_message_with_buttons(message, buttons)
    log.info("Requested approval [%s] for '%s' [%s]", short_id, signal.market_question, signal.outcome)


def _execute_approved_trade(pending_record: dict) -> str:
    """Runs every real-money risk check fresh (not reusing anything from
    when approval was first requested, since balance/exposure can have
    changed since then) and places the order if everything checks out.
    Returns a short human-readable result string for the Telegram reply.
    """
    import execution  # lazy import — only needed once we're actually trading real money

    market_id = pending_record["market_id"]
    outcome = pending_record["outcome"]
    token_id = pending_record["token_id"]

    if risk_manager.has_open_trade(market_id, outcome, mode="live"):
        return "Already have an open live position on this — no action taken."

    if risk_manager.daily_loss_cap_reached():
        return "Daily loss cap already reached — no action taken."

    if not token_id:
        return "No token ID available for this market — cannot place a real order."

    try:
        balance = execution.get_wallet_balance_usd()
    except Exception as e:
        log.error("Could not fetch wallet balance: %s", e)
        return f"Could not check wallet balance ({e}) — no action taken."

    size_usd = risk_manager.compute_trade_size_usd(balance)
    if size_usd < 1.0:
        return (f"Blocked by the 26% total exposure cap (balance ${balance:,.2f}) "
                f"— no action taken.")

    try:
        resp = execution.place_market_buy(token_id, size_usd)
    except Exception as e:
        log.error("Order placement failed: %s", e)
        return f"Order placement failed ({e})."

    entry_price = resp.get("price", 0) if isinstance(resp, dict) else 0
    risk_manager.record_trade_open(
        market_id, pending_record["market_question"], outcome,
        size_usd, entry_price, pending_record["category"], mode="live"
    )
    return f"✅ Executed: ${size_usd:,.2f} on '{outcome}' (balance was ${balance:,.2f})."


def _expire_stale_approvals(pending: dict) -> dict:
    now = datetime.now(timezone.utc)
    still_pending = {}
    for short_id, record in pending.items():
        requested_at = datetime.fromisoformat(record["requested_at"])
        age_minutes = (now - requested_at).total_seconds() / 60
        if age_minutes > APPROVAL_TIMEOUT_MINUTES:
            log.info("Approval [%s] for '%s' expired unanswered after %.0f min.",
                      short_id, record["market_question"], age_minutes)
            send_telegram_alert(
                f"⏱️ Approval request for '{record['market_question']}' expired "
                f"unanswered — no action taken."
            )
        else:
            still_pending[short_id] = record
    return still_pending


def process_pending_approvals() -> None:
    """Call this once per cycle. Polls for button taps, executes/drops
    accordingly, and expires anything too old to still be relevant."""
    last_update_id = _load_last_update_id()
    updates = get_telegram_updates(offset=last_update_id + 1)

    pending = _load_pending()
    highest_seen = last_update_id

    for update in updates:
        highest_seen = max(highest_seen, update.get("update_id", 0))
        callback = update.get("callback_query")
        if not callback:
            continue

        data = callback.get("data", "")
        callback_id = callback.get("id")
        if ":" not in data:
            answer_callback_query(callback_id)
            continue

        action, short_id = data.split(":", 1)
        record = pending.get(short_id)
        if not record:
            # Already handled in a previous cycle, or expired, or unknown.
            answer_callback_query(callback_id, text="This request is no longer active.")
            continue

        if action == "approve":
            result = _execute_approved_trade(record)
            answer_callback_query(callback_id, text="Processing...")
            send_telegram_alert(f"*{record['market_question']}*\n{result}")
            del pending[short_id]
        elif action == "reject":
            answer_callback_query(callback_id, text="Rejected.")
            send_telegram_alert(f"❌ Rejected: {record['market_question']}")
            del pending[short_id]
        else:
            answer_callback_query(callback_id)

    pending = _expire_stale_approvals(pending)
    _save_pending(pending)

    if highest_seen > last_update_id:
        _save_last_update_id(highest_seen)
