"""
elite_movers.py — alert whenever one of the top-5 overall-ranked traders
takes ANY new position, regardless of whether anyone else agrees with them.

This is deliberately different from category consensus (needs 4+ agreement)
and early movers (needs 2+ on a brand-new market): a single elite-ranked
trader moving is worth knowing about on its own — no threshold, no
agreement required. "Top 5" means overall rank in the candidate pool (by
profit/volume), not rank within any one category.

PRICE FLOOR ADDED (2026-11-04, Levi's request, data-driven): the
ELITE_TRADE_MIN_RESOLVED tightening in wallet_tracker.py (2026-11-03,
2->5 resolved picks before trusting a wallet's category win rate) did NOT
fix Elite Mover's accuracy — a post-fix sample of 72 resolved paper trades
still came in at 31.9% win rate, -$0.44/unit avg P&L. Breaking that same
72-trade sample down by entry price (trade_optimizer.py's Elite-Mover-only
price band report) showed the real pattern was never about wallet
quality at all:
    0-20%:  0/16 (0%),    avg P&L $-1.00/unit  — total wipeout
    20-40%: 4/19 (21.1%), avg P&L $-0.36/unit
    40-60%: 5/20 (25.0%), avg P&L $-0.50/unit
    60-80%: 7/10 (70.0%), avg P&L $-0.03/unit
    80-100%: 7/7 (100%),  avg P&L $+0.16/unit
55 of 72 trades (every band under 60%) were net losers; every band at or
above 60% was breakeven-or-better. A top-5 wallet reinforcing an
already-likely outcome and a top-5 wallet taking a cheap, long-shot
position were being treated as the same signal strength by every quality
gate this module and wallet_tracker.py had (win rate, streak, conviction)
— none of them looked at price at all, which is exactly the blind spot
Consensus and Early Movers had before CONSENSUS_PRICE_BAND and
EARLY_MOVER_PRICE_BAND were added for them. Unlike those two bands,
ELITE_MOVER_MIN_PRICE is a floor only, not a band — the 80-100% range is
this signal's BEST-performing slice (not a low-payout zone to avoid the
way it is for Consensus/Early Movers), so there's deliberately no upper
bound here.

This filter sits in find_elite_moves() itself, not just in
wallet_tracker.passes_elite_trade_filter()'s paper-trade gate — a cheap,
low-price move now never becomes a "move" at all, so it generates no
Telegram alert and no live Approve/Reject prompt either, not just a
skipped paper log. That's a deliberately bigger blast radius than the
2026-11-03 fix: that one only cleaned up the paper-trade accuracy number
while Levi would still have been getting live approval prompts for the
same bad low-price signals. Threshold picked at 0.60 — the natural split
point in the data above, where the 40-60% bucket's clear loss gives way to
the 60-80% bucket's near-breakeven performance.
"""

from category_leaderboard import EXCLUDED_CATEGORIES

TOP_N_ELITE = 5

ELITE_MOVER_MIN_PRICE = 0.60  # see module docstring — floor only, no upper bound


def find_elite_moves(data: dict) -> list[dict]:
    """Returns a list of dicts, one per new position taken by a top-5
    overall-ranked wallet this cycle, priced at or above
    ELITE_MOVER_MIN_PRICE: {wallet, rank, market_id, market_question,
    outcome, size_usd, category, token_id, end_date, cur_price, event_id}.
    Empty list if none. token_id/end_date/cur_price are what let a move be
    turned into a tradeable ConsensusSignal downstream (see
    consensus_bot.py's run_elite_movers).
    """
    wallet_rank = data["wallet_overall_rank"]
    category_map = data["category_map"]
    moves = []

    for p in data.get("newly_observed_positions", []):
        rank = wallet_rank.get(p.wallet)
        if rank is None or rank > TOP_N_ELITE:
            continue

        if p.cur_price < ELITE_MOVER_MIN_PRICE:
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
            "event_slug": p.event_slug,
        })

    moves.sort(key=lambda m: m["rank"])
    return moves
