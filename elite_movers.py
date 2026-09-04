"""
elite_movers.py — alert whenever one of the top-5 overall-ranked traders
takes ANY new position, regardless of whether anyone else agrees with them.

This is deliberately different from category consensus (needs 4+ agreement)
and early movers (needs 2+ on a brand-new market): a single elite-ranked
trader moving is worth knowing about on its own — no threshold, no
agreement required. "Top 5" means overall rank in the candidate pool (by
profit/volume), not rank within any one category.
"""

from category_leaderboard import EXCLUDED_CATEGORIES

TOP_N_ELITE = 5


def find_elite_moves(data: dict) -> list[dict]:
    """Returns a list of dicts, one per new position taken by a top-5
    overall-ranked wallet this cycle: {wallet, rank, market_id,
    market_question, outcome, size_usd, category}. Empty list if none.
    """
    wallet_rank = data["wallet_overall_rank"]
    category_map = data["category_map"]
    moves = []

    for p in data.get("newly_observed_positions", []):
        rank = wallet_rank.get(p.wallet)
        if rank is None or rank > TOP_N_ELITE:
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
        })

    moves.sort(key=lambda m: m["rank"])
    return moves
