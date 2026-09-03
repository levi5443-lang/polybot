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


def format_consensus_message(signal, total_tracked: int, wallet_ranks: dict = None,
                              pool_size: int = None) -> str:
    """Build a readable alert message from a ConsensusSignal."""
    return (
        f"*Polymarket Consensus Signal*  _[{signal.category}]_\n\n"
        f"Market: {signal.market_question}\n"
        f"Outcome: *{signal.outcome}*\n"
        f"Agreement: {signal.count}/{total_tracked} tracked top traders\n"
        f"{_format_rank_line(signal, wallet_ranks, pool_size)}"
        f"Aggregate size: ${signal.total_size_usd:,.0f}\n"
    )


def format_early_mover_message(signal, wallet_ranks: dict = None, pool_size: int = None) -> str:
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
        f"Aggregate size: ${signal.total_size_usd:,.0f}\n"
    )
