"""
wallet_tracker.py — per-wallet, per-category track record.

Every cycle, every candidate wallet's positions get logged here (not just
wallets that end up part of a fired signal) — the goal is to have real
history on a trader BEFORE they ever show up in a signal, not start
counting from zero the first time they do.

Records are keyed by (wallet, market_id, outcome) so the same open
position seen across many cycles is only ever logged once. Resolution
checking works the same way as trade_tracker.py — cross-reference against
Polymarket's resolved markets — but this tracks OTHER traders' accuracy,
not the bot's own trades.
"""

import json
import logging
from datetime import datetime, timezone

from consensus_logic import Position
from polymarket_api import check_market_resolution, polite_sleep
from storage_paths import persistent_path

log = logging.getLogger("wallet_tracker")

WALLET_RECORDS_FILE = persistent_path("wallet_records.json")

# Don't show a win rate percentage until a wallet has at least this many
# RESOLVED positions in that category — otherwise early noise (e.g. 1-0,
# "100%") looks like a meaningful signal when it's just one data point.
MIN_SAMPLE_SIZE = 5


def _load_records() -> dict:
    try:
        with open(WALLET_RECORDS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_records(records: dict) -> None:
    try:
        with open(WALLET_RECORDS_FILE, "w") as f:
            json.dump(records, f)
    except OSError as e:
        log.error("Could not save wallet records: %s", e)


def _record_key(wallet: str, market_id: str, outcome: str) -> str:
    return f"{wallet}|{market_id}|{outcome}"


def record_observed_positions(all_positions: list[Position], category_map: dict) -> int:
    """Log every candidate wallet's current positions. Returns how many
    genuinely new positions got logged (already-known ones are skipped —
    this is safe to call every cycle without creating duplicates)."""
    records = _load_records()
    new_count = 0

    for p in all_positions:
        key = _record_key(p.wallet, p.market_id, p.outcome)
        if key in records:
            continue  # already tracking this exact position

        category = category_map.get(p.event_id, "Uncategorized")
        records[key] = {
            "wallet": p.wallet,
            "market_id": p.market_id,
            "market_question": p.market_question,
            "outcome": p.outcome,
            "category": category,
            "first_seen_at": datetime.now(timezone.utc).isoformat(),
            "status": "open",
            "actual_outcome": None,
            "correct": None,
            "resolved_at": None,
        }
        new_count += 1

    if new_count:
        _save_records(records)
        log.info("Wallet tracker: logged %d newly-observed position(s).", new_count)

    return new_count


def sync_wallet_resolutions() -> int:
    """Check open records against THEIR OWN specific markets, directly —
    not a paginated list of "recently closed" markets (that approach
    silently missed real resolutions). Returns how many got resolved
    this pass."""
    records = _load_records()
    open_keys = [k for k, r in records.items() if r["status"] == "open"]
    if not open_keys:
        return 0

    # Many wallets often hold the same popular market — only check each
    # distinct market once per pass, not once per wallet holding it.
    unique_market_ids = list(dict.fromkeys(records[k]["market_id"] for k in open_keys))
    resolution_by_market: dict[str, str] = {}
    for market_id in unique_market_ids:
        outcome = check_market_resolution(market_id)
        if outcome:
            resolution_by_market[market_id] = outcome
        polite_sleep(0.1)

    resolved_count = 0
    for key in open_keys:
        r = records[key]
        actual_outcome = resolution_by_market.get(r["market_id"])
        if actual_outcome is None:
            continue

        r["status"] = "closed"
        r["actual_outcome"] = actual_outcome
        r["correct"] = (r["outcome"] == actual_outcome)
        r["resolved_at"] = datetime.now(timezone.utc).isoformat()
        resolved_count += 1

    if resolved_count:
        _save_records(records)
        log.info("Wallet tracker: resolved %d position(s) this pass.", resolved_count)

    return resolved_count


def get_wallet_category_record(wallet: str, category: str) -> dict:
    """Wins/losses for one wallet within one category, among CLOSED
    (resolved) positions only."""
    records = _load_records()
    closed = [
        r for r in records.values()
        if r["wallet"] == wallet and r["category"] == category and r["status"] == "closed"
    ]
    wins = sum(1 for r in closed if r["correct"])
    total = len(closed)
    return {
        "wins": wins,
        "losses": total - wins,
        "total_resolved": total,
        "win_rate_pct": round(100 * wins / total, 1) if total > 0 else None,
    }


def format_wallet_record(wallet: str, category: str) -> str:
    """Short display string for a Telegram message, e.g. '12-4, 75%',
    'new' (zero resolved), or '3 resolved, too early' (below MIN_SAMPLE_SIZE)."""
    rec = get_wallet_category_record(wallet, category)
    if rec["total_resolved"] == 0:
        return "new"
    if rec["total_resolved"] < MIN_SAMPLE_SIZE:
        return f"{rec['total_resolved']} resolved, too early"
    return f"{rec['wins']}-{rec['losses']}, {rec['win_rate_pct']}%"
