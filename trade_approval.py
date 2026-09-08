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

TEXT COMMANDS (/positions, /history, /accuracy, /resetpaper, /help): these
used to be handled by a Telegram WEBHOOK in webapp/main.py. A webhook and
this worker's own getUpdates polling can't both be registered on the same
bot at once — Telegram returns 409 Conflict if you try — so once the
webhook got deleted (to fix that exact 409 blocking Approve/Reject taps),
those commands stopped getting any reply. Rather than re-adding the
webhook and reintroducing that conflict, process_pending_approvals() below
now also handles plain-text messages itself, reusing the exact same
trade_tracker/risk_manager functions the webhook used to call. Same
tradeoff as button taps: a command's reply can take up to
POLL_INTERVAL_SECONDS to arrive, since it's picked up on the worker's next
cycle rather than instantly.
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
import trade_tracker
import os

log = logging.getLogger("trade_approval")

PENDING_APPROVALS_KEY = "pending_approvals.json"
LAST_UPDATE_ID_KEY = "telegram_last_update_id.json"
APPROVAL_TIMEOUT_MINUTES = 60

HELP_TEXT = (
    "Available commands:\n"
    "/positions — currently open trades\n"
    "/history — most recent resolved trades\n"
    "/accuracy — overall win rate + ROI + open trade count\n"
    "/resetpaper — wipe paper trade history (asks to confirm first)\n"
    "/wallet — check which wallet the bot is trading from + live balance\n"
    "/help — this message"
)


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


def _format_resolution_date(end_date: str) -> str:
    """Turns Polymarket's raw endDate into something readable, e.g.
    'Sep 15, 2026'. Returns 'unknown' if the date is missing or malformed
    — never lets a formatting hiccup block the whole approval message."""
    if not end_date:
        return "unknown"
    try:
        dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
        return dt.strftime("%b %d, %Y")
    except (ValueError, TypeError):
        return end_date  # show the raw value rather than hiding it entirely


def request_trade_approval(signal, signal_type: str = "consensus") -> None:
    """Send an Approve/Reject prompt for a live signal and remember it.

    signal_type ('consensus', 'early_mover', 'elite_mover') is carried in
    the pending record so that, if you approve, _execute_approved_trade()
    below can log the trade with the signal type that actually produced
    it — otherwise every live trade would get stamped 'consensus' by
    default regardless of where it came from."""
    short_id = uuid.uuid4().hex[:8]
    pending = _load_pending()
    pending[short_id] = {
        "market_id": signal.market_id,
        "outcome": signal.outcome,
        "market_question": signal.market_question,
        "category": signal.category,
        "token_id": signal.token_id,
        "end_date": signal.end_date,
        "signal_type": signal_type,
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_pending(pending)

    record_str = wallet_tracker.format_wallet_record(
        signal.agreeing_wallets[0], signal.category
    ) if signal.agreeing_wallets else None
    record_line = f"\n{signal.category} record (lead trader): {record_str}" if record_str else ""

    resolution_line = f"Expected resolution: {_format_resolution_date(signal.end_date)}\n"

    return_pct = signal.expected_return_pct
    if return_pct > 0:
        return_line = f"Return if correct: +{return_pct}% (buying at ${signal.cur_price:.2f})\n"
        # A $ estimate is a genuine convenience, but it's inherently a
        # PREVIEW — the real size gets recomputed fresh (fresh balance,
        # fresh 26% exposure check) at the moment you actually approve,
        # so this number can differ slightly from what actually executes.
        try:
            import execution
            balance = execution.get_wallet_balance_usd()
            preview_size = risk_manager.get_position_size_usd(balance)
            preview_profit = preview_size * (return_pct / 100)
            return_line += (f"Est. profit: ~${preview_profit:,.2f} on a ~${preview_size:,.2f} "
                             f"position (preview — recalculated fresh if you approve)\n")
        except Exception as e:
            log.warning("Could not compute a preview \\$ estimate: %s", e)
    else:
        return_line = ""

    message = (
        f"🔔 *APPROVAL NEEDED*  _[{signal.category}]_\n\n"
        f"Market: {signal.market_question}\n"
        f"Side: *{signal.outcome}*\n"
        f"Agreement: {signal.count} tracked top traders{record_line}\n"
        f"{resolution_line}"
        f"{return_line}\n"
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

    if risk_manager.has_ever_traded(market_id, outcome, mode="live"):
        return "Already traded this market before (open or resolved) — no action taken."

    # Daily realized-loss cap check removed here (2026-09-08, Levi's
    # request) — no longer blocks approved trades regardless of today's
    # realized losses. NOTE: this call site was left behind pointing at
    # risk_manager.daily_loss_cap_reached(), which had already been
    # deleted from risk_manager.py — every Approve tap since then raised
    # an AttributeError right here, before the order could ever be
    # placed or a confirmation sent. That's the actual root cause of
    # "nothing happens" on Approve/Reject (2026-09-08 fix).

    if not token_id:
        return "No token ID available for this market — cannot place a real order."

    try:
        balance = execution.get_wallet_balance_usd()
    except Exception as e:
        log.error("Could not fetch wallet balance: %s", e)
        return f"Could not check wallet balance ({e}) — no action taken."

    size_usd = risk_manager.compute_trade_size_usd(balance)
    if size_usd < 1.0:
        # Includes which wallet address the bot is actually checking —
        # added 2026-09-08 so a wrong/mismatched POLYMARKET_PRIVATE_KEY is
        # visible right here in Telegram instead of needing server logs
        # pulled every time this comes up. If POLYMARKET_WALLET_ADDRESS is
        # also set (Levi's known-funded wallet), this now says explicitly
        # whether the key actually matches it — read-only, never touches
        # Render's env.
        status = execution.get_wallet_status()
        base = (f"Blocked by the 26% total exposure cap (balance ${balance:,.2f} "
                f"on wallet {status['resolved']}) — no action taken.")
        if status["mismatch"] is True:
            base += (f"\n\n⚠️ This does NOT match the funded wallet you told the bot "
                      f"to expect ({status['expected']}). The key in Render still "
                      f"isn't the right one — it controls a different, unfunded "
                      f"wallet. Balance will stay $0.00 until the key actually "
                      f"matches that address.")
        elif status["mismatch"] is False:
            base += "\n\n✅ This matches your funded wallet — the key is correct."
        return base

    try:
        resp = execution.place_market_buy(token_id, size_usd)
    except Exception as e:
        log.error("Order placement failed: %s", e)
        return f"Order placement failed ({e})."

    entry_price = resp.get("price", 0) if isinstance(resp, dict) else 0
    risk_manager.record_trade_open(
        market_id, pending_record["market_question"], outcome,
        size_usd, entry_price, pending_record["category"], mode="live",
        signal_type=pending_record.get("signal_type", "consensus"),
        end_date=pending_record.get("end_date", "")
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


def _handle_text_command(text: str) -> None:
    """Mirrors the /positions, /history, /accuracy, /resetpaper, /help
    handling that used to live in webapp/main.py's Telegram webhook,
    before that webhook was deleted to fix the getUpdates 409 conflict.
    Sends its reply the same way approve/reject confirmations are sent."""
    text = text.strip()
    if text.startswith("/positions"):
        reply = trade_tracker.format_open_positions_message()
    elif text.startswith("/history"):
        reply = trade_tracker.format_history_message()
    elif text.startswith("/accuracy"):
        reply = trade_tracker.format_accuracy_command_message()
    elif text.startswith("/resetpaper confirm"):
        removed = risk_manager.reset_paper_trades()
        reply = f"✅ Cleared {removed} paper trade(s). Starting fresh."
    elif text.startswith("/resetpaper"):
        reply = ("⚠️ This will permanently delete ALL paper trade history "
                  "(open and resolved) — accuracy, ROI, everything. Live trades "
                  "are never affected.\n\nSend /resetpaper confirm to proceed, "
                  "or ignore this to cancel.")
    elif text.startswith("/wallet") or text.startswith("/balance"):
        reply = _handle_wallet_command()
    elif text.startswith("/help") or text.startswith("/start"):
        reply = HELP_TEXT
    else:
        reply = f"Unknown command.\n\n{HELP_TEXT}"
    send_telegram_alert(reply)


def _handle_wallet_command() -> str:
    """/wallet (or /balance) — on-demand wallet check, added 2026-09-08 so
    Levi can verify which wallet the bot is actually trading from and its
    live balance at any time, instead of waiting for a signal to fire and
    get blocked. Read-only — never touches Render's environment, only
    reads POLYMARKET_PRIVATE_KEY / POLYMARKET_WALLET_ADDRESS and asks
    Polymarket for the current balance."""
    if os.environ.get("PAPER_MODE", "").lower() in ("true", "1", "yes"):
        return "⚠️ PAPER_MODE is on — no real wallet is in use."

    import execution

    status = execution.get_wallet_status()
    lines = [f"Wallet in use: {status['resolved']}"]

    if status["expected"] is None:
        lines.append(
            "(Set POLYMARKET_WALLET_ADDRESS in Render to your funded wallet "
            "and this will tell you automatically whether the key matches it.)"
        )
    elif status["mismatch"] is True:
        lines.append(f"⚠️ Does NOT match your expected wallet ({status['expected']}).")
    elif status["mismatch"] is False:
        lines.append("✅ Matches your expected wallet.")

    try:
        balance = execution.get_wallet_balance_usd()
        lines.append(f"Balance: ${balance:,.2f}")
    except Exception as e:
        lines.append(f"Could not fetch balance right now: {e}")

    return "\n".join(lines)


def process_pending_approvals() -> None:
    """Call this once per cycle. Polls for button taps AND text commands
    (/positions, /history, /accuracy, /resetpaper, /help), executes/drops
    approvals accordingly, and expires anything too old to still be
    relevant."""
    last_update_id = _load_last_update_id()
    updates = get_telegram_updates(offset=last_update_id + 1)

    pending = _load_pending()
    highest_seen = last_update_id
    authorized_chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    for update in updates:
        highest_seen = max(highest_seen, update.get("update_id", 0))

        message = update.get("message")
        if message is not None:
            chat_id = str(message.get("chat", {}).get("id", ""))
            text = (message.get("text") or "").strip()
            # Log every text message the instant it's seen, before the
            # authorization check — added 2026-09-08 because, unlike
            # button taps (which already log "Callback received"
            # unconditionally), a text command that didn't match
            # authorized_chat_id — or that raised an exception inside
            # _handle_text_command — vanished with ZERO trace anywhere.
            # This makes that failure mode visible instead of silent.
            if text:
                log.info("Message received: text=%r from chat_id=%s (authorized=%s)",
                          text, chat_id, authorized_chat_id)
            if text and authorized_chat_id and chat_id == str(authorized_chat_id):
                try:
                    _handle_text_command(text)
                except Exception as e:
                    log.error("Text command %r raised an exception: %s", text, e, exc_info=True)
                    send_telegram_alert(f"⚠️ Internal error handling that command: {e}")
            elif text and not authorized_chat_id:
                log.warning("TELEGRAM_CHAT_ID is not set in the environment — "
                            "no chat is authorized, so this command was ignored.")
            elif text:
                log.warning("Message from chat_id=%s ignored — does not match "
                            "authorized TELEGRAM_CHAT_ID=%s.", chat_id, authorized_chat_id)
            continue

        callback = update.get("callback_query")
        if not callback:
            continue

        data = callback.get("data", "")
        callback_id = callback.get("id")
        # Log every button tap the instant it's seen, before any other check
        # — this is the one line that proves Telegram actually delivered the
        # tap to the bot at all, regardless of what happens to it next.
        # Added 2026-09-08 (Levi's request) because taps that hit an old/
        # stale message were being silently dropped with nothing in the
        # logs to show they'd even arrived.
        log.info("Callback received: data=%r from user_id=%s", data,
                  callback.get("from", {}).get("id"))
        if ":" not in data:
            answer_callback_query(callback_id)
            continue

        action, short_id = data.split(":", 1)
        record = pending.get(short_id)
        if not record:
            log.info("Callback [%s] ignored: no pending request with that ID "
                      "(expired, already handled, or an old message) — "
                      "currently pending: %s", short_id, list(pending.keys()))
            answer_callback_query(callback_id, text="This request is no longer active.")
            continue

        if action == "approve":
            log.info("Approve tapped [%s] for '%s' [%s] — executing...",
                      short_id, record["market_question"], record["outcome"])
            try:
                result = _execute_approved_trade(record)
            except Exception as e:
                # This is exactly the class of bug that caused "nothing
                # happens" before (2026-09-08): an uncaught exception here
                # used to blow up the whole poll cycle silently — no
                # confirmation sent, pending entry never cleared, no clear
                # log line. Now it's caught, logged with the real error,
                # and you still get a Telegram message so you know it failed
                # instead of just seeing the button do nothing.
                log.error("Approve [%s] for '%s' raised an exception: %s",
                           short_id, record["market_question"], e, exc_info=True)
                result = f"⚠️ Internal error while executing this trade: {e}"
            log.info("Approve [%s] result: %s", short_id, result)
            answer_callback_query(callback_id, text="Processing...")
            send_telegram_alert(f"*{record['market_question']}*\n{result}")
            del pending[short_id]
        elif action == "reject":
            log.info("Reject tapped [%s] for '%s' [%s]",
                      short_id, record["market_question"], record["outcome"])
            answer_callback_query(callback_id, text="Rejected.")
            send_telegram_alert(f"❌ Rejected: {record['market_question']}")
            del pending[short_id]
        else:
            answer_callback_query(callback_id)

    pending = _expire_stale_approvals(pending)
    _save_pending(pending)

    if highest_seen > last_update_id:
        _save_last_update_id(highest_seen)
