"""
telegram_alert.py — minimal Telegram alerting, no extra dependencies.

Set these env vars (same pattern as your other Telegram bots):
    TELEGRAM_BOT_TOKEN   - from @BotFather
    TELEGRAM_CHAT_ID     - the chat/channel/group ID to post alerts to

Create a separate bot via @BotFather for this project rather than reusing the
apex_signal_bot token, so a bug here can't affect your live signal channel.
"""

import os
import logging
import requests

log = logging.getLogger("telegram_alert")

TELEGRAM_API_BASE = "https://api.telegram.org"


def send_telegram_alert(message: str, bot_token: str = None, chat_id: str = None) -> bool:
    """Send a message via Telegram Bot API. Returns True on success."""
    bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        log.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set — skipping alert:\n%s", message)
        return False

    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        log.error("Failed to send Telegram alert: %s", e)
        return False


def send_telegram_message_with_buttons(message: str, buttons: list[list[dict]],
                                        bot_token: str = None, chat_id: str = None) -> bool:
    """Send a message with inline buttons attached (e.g. Approve/Reject).
    buttons is a list of rows, each row a list of {"text": ..., "callback_data": ...}
    dicts — matches Telegram's InlineKeyboardMarkup shape directly."""
    bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        log.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set — skipping button message:\n%s", message)
        return False

    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": buttons},
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        log.error("Failed to send Telegram button message: %s", e)
        return False


def get_telegram_updates(offset: int = None, bot_token: str = None, timeout: int = 0) -> list[dict]:
    """Poll Telegram for updates (messages, button taps) since `offset`.
    Returns an empty list on any failure — a polling hiccup should never
    crash a poll cycle."""
    bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        return []

    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/getUpdates"
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset

    try:
        resp = requests.get(url, params=params, timeout=timeout + 10)
        resp.raise_for_status()
        return resp.json().get("result", [])
    except requests.RequestException as e:
        log.error("Failed to fetch Telegram updates: %s", e)
        return []


def answer_callback_query(callback_query_id: str, text: str = None,
                           bot_token: str = None) -> bool:
    """Acknowledge a button tap — Telegram shows a loading spinner on the
    user's client until this is called, regardless of whether there's
    anything to actually tell them."""
    bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        return False

    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text

    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        log.error("Failed to answer callback query: %s", e)
        return False


def _format_rank_line(signal, wallet_ranks: dict = None, pool_size: int = None) -> str:
    """e.g. 'Overall ranks (of 50 tracked): #3, #7, #22, #41' — shows where
    the specific agreeing wallets sit in the full candidate pool, not just
    how many agreed. #3 and #7 agreeing is a very different signal than
    #3 and #91 agreeing, even though both are "2 traders"."""
    if not wallet_ranks:
        return ""
    ranks = sorted(wallet_ranks[w] for w in signal.agreeing_wallets if w in wallet_ranks)
    if not ranks:
        return ""
    ranks_str = ", ".join(f"#{r}" for r in ranks)
    pool_note = f" (of {pool_size} tracked)" if pool_size else ""
    return f"Overall ranks{pool_note}: {ranks_str}\n"


def _format_track_record_line(signal, wallet_ranks: dict = None, wallet_records: dict = None) -> str:
    """e.g. 'Sports record: #3 (12-4, 75%) | #17 (new) | #44 (6-9, 40%)' —
    each agreeing wallet's OWN accuracy history in this specific category,
    not their overall profit rank. wallet_records is {wallet: record_str},
    built by the caller via wallet_tracker.format_wallet_record()."""
    if not wallet_ranks or not wallet_records:
        return ""
    wallets_sorted = sorted(
        (w for w in signal.agreeing_wallets if w in wallet_ranks and w in wallet_records),
        key=lambda w: wallet_ranks[w]
    )
    if not wallets_sorted:
        return ""
    parts = [f"#{wallet_ranks[w]} ({wallet_records[w]})" for w in wallets_sorted]
    return f"{signal.category} record: " + " | ".join(parts) + "\n"


def format_consensus_message(signal, total_tracked: int, wallet_ranks: dict = None,
                              pool_size: int = None, wallet_records: dict = None) -> str:
    """Build a readable alert message from a ConsensusSignal."""
    return (
        f"*Polymarket Consensus Signal*  _[{signal.category}]_\n\n"
        f"Market: {signal.market_question}\n"
        f"Outcome: *{signal.outcome}*\n"
        f"Agreement: {signal.count}/{total_tracked} tracked top traders\n"
        f"{_format_rank_line(signal, wallet_ranks, pool_size)}"
        f"{_format_track_record_line(signal, wallet_ranks, wallet_records)}"
        f"Aggregate size: ${signal.total_size_usd:,.0f}\n"
    )


def format_early_mover_message(signal, wallet_ranks: dict = None, pool_size: int = None,
                                wallet_records: dict = None) -> str:
    """Build a distinctly-labeled alert for a brand-new market where
    multiple tracked wallets are already positioned. Deliberately doesn't
    show "X/total_tracked" like the regular consensus message — early
    movers are about a market being new, not about a leaderboard size."""
    return (
        f"🚀 *EARLY MOVER*  _[{signal.category}]_\n\n"
        f"Brand-new market: {signal.market_question}\n"
        f"Side: *{signal.outcome}*\n"
        f"{signal.count} tracked top traders already in\n"
        f"{_format_rank_line(signal, wallet_ranks, pool_size)}"
        f"{_format_track_record_line(signal, wallet_ranks, wallet_records)}"
        f"Aggregate size: ${signal.total_size_usd:,.0f}\n"
    )


def format_elite_mover_message(move: dict, pool_size: int = None, record_str: str = None) -> str:
    """Build an alert for a single top-5 overall-ranked trader taking a
    new position — no agreement/threshold involved, just "this specific
    elite trader just moved." Shows the wallet's own stats (category
    track record) so you can judge the move on its own merits."""
    pool_note = f" (of {pool_size} tracked)" if pool_size else ""
    record_line = f"{move['category']} record: {record_str}\n" if record_str else ""
    return (
        f"⭐ *TOP {move['rank']} TRADER MOVE*  _[{move['category']}]_\n\n"
        f"Overall rank: #{move['rank']}{pool_note}\n"
        f"Market: {move['market_question']}\n"
        f"Side: *{move['outcome']}*\n"
        f"{record_line}"
        f"Position size: ${move['size_usd']:,.0f}\n"
    )
