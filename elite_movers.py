"""
elite_movers.py — alert whenever one of the top-5 overall-ranked traders
takes ANY new position, regardless of whether anyone else agrees with them.

This is deliberately different from category consensus (needs 4+ agreement)
and early movers (needs 2+ on a brand-new market): a single elite-ranked
trader moving is worth knowing about on its own — no threshold, no
agreement required. "Top 5" means overall rank in the candidate pool (by
profit/volume), not rank within any one category.

A wallet also needs at least MIN_ELITE_RESOLVED_TRADES resolved positions
(the bot's own tracked history, same threshold wallet_tracker uses
elsewhere) before it's eligible to be treated as elite at all — added at
Levi's request (2026-09-08). This is a HARD requirement on the wallet
itself, separate from rank: wallet_overall_rank's own ranking already
deprioritizes unproven wallets (see
wallet_tracker.rank_wallets_by_realized_roi), but if fewer than 5 wallets
in the whole candidate pool have 5+ resolved trades, unproven wallets
would otherwise backfill the remaining rank-1-through-5 slots and still
fire signals. This check closes that gap: no wallet under the resolved-
trade minimum can ever produce an elite-mover signal, regardless of what
numeric rank it's been assigned.
"""

from category_leaderboard import EXCLUDED_CATEGORIES
import wallet_tracker

TOP_N_ELITE = 5
MIN_ELITE_RESOLVED_TRADES = 5


def find_elite_moves(data: dict) -> list[dict]:
    """Returns a list of dicts, one per new position taken by a top-5
    overall-ranked wallet this cycle (and only once that wallet has at
    least MIN_ELITE_RESOLVED_TRADES resolved positions on record):
    {wallet, rank, market_id, market_question, outcome, size_usd,
    category, token_id, end_date, cur_price, event_id}. Empty list if
    none. token_id/end_date/cur_price are what let a move be turned into
    a tradeable ConsensusSignal downstream (see consensus_bot.py's
    run_elite_movers).
    """
    wallet_rank = data["wallet_overall_rank"]
    category_map = data["category_map"]
    moves = []

    for p in data.get("newly_observed_positions", []):
        rank = wallet_rank.get(p.wallet)
        if rank is None or rank > TOP_N_ELITE:
            continue

        resolved_count = wallet_tracker.get_wallet_realized_roi(p.wallet)["resolved_count"]
        if resolved_count < MIN_ELITE_RESOLVED_TRADES:
            continue

        category = category_map.get(p.event_id, "Uncategorized")
        if category in EXCLUDED_CATEGORIES:
            continue

        moves.append({
            "wallet": p.wallet,
            "rank": rank,
            "market_id": p.market_id,
            "market_question": p.market_question,
            "outcome": p.outcome,
            "size_usd": p.size_usd,
            "category": category,
            "token_id": p.token_id,
            "end_date": p.end_date,
            "cur_price": p.cur_price,
            "event_id": p.event_id,
        })

    moves.sort(key=lambda m: m["rank"])
    return moves
