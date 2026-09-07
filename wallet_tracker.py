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

import logging
from datetime import datetime, timezone
from collections import defaultdict

from consensus_logic import Position
from polymarket_api import check_market_resolution, polite_sleep
from shared_storage import get_json, set_json

log = logging.getLogger("wallet_tracker")

WALLET_RECORDS_KEY = "wallet_records.json"

# Don't show a win rate percentage until a wallet has at least this many
# RESOLVED positions in that category — otherwise early noise (e.g. 1-0,
# "100%") looks like a meaningful signal when it's just one data point.
MIN_SAMPLE_SIZE = 5


def _load_records() -> dict:
    return get_json(WALLET_RECORDS_KEY, {})


def _save_records(records: dict) -> None:
    set_json(WALLET_RECORDS_KEY, records)


def _record_key(wallet: str, market_id: str, outcome: str) -> str:
    return f"{wallet}|{market_id}|{outcome}"


def record_observed_positions(all_positions: list[Position], category_map: dict) -> list[Position]:
    """Log every candidate wallet's current positions. Returns the
    genuinely NEW positions logged this cycle (already-known ones are
    skipped — safe to call every cycle without duplicates). Returning the
    actual positions, not just a count, is what lets callers build
    per-wallet alerts (e.g. "a top-5 trader just took a new position")."""
    records = _load_records()
    new_positions = []

    # Each wallet's current total portfolio, from THIS cycle's full
    # snapshot — used to record what % of their portfolio each NEW
    # position represents at the moment it's first observed. Building
    # this up over time is what eventually lets us compare a wallet's
    # CURRENT bet size against their own historical norm, instead of
    # just showing a concentration number with no context for whether
    # it's unusual for THEM specifically.
    wallet_totals = defaultdict(float)
    for p in all_positions:
        wallet_totals[p.wallet] += p.size_usd

    for p in all_positions:
        key = _record_key(p.wallet, p.market_id, p.outcome)
        if key in records:
            continue  # already tracking this exact position

        category = category_map.get(p.event_id, "Uncategorized")
        wallet_total = wallet_totals.get(p.wallet, 0)
        concentration_at_entry = round(100 * p.size_usd / wallet_total, 1) if wallet_total > 0 else None

        records[key] = {
            "wallet": p.wallet,
            "market_id": p.market_id,
            "market_question": p.market_question,
            "outcome": p.outcome,
            "category": category,
            "size_usd": p.size_usd,
            "entry_price": p.cur_price,  # price at the moment we first saw this position
            "concentration_pct_at_entry": concentration_at_entry,
            "first_seen_at": datetime.now(timezone.utc).isoformat(),
            "status": "open",
            "actual_outcome": None,
            "correct": None,
            "resolved_at": None,
            "realized_pnl_usd": None,
        }
        new_positions.append(p)

    if new_positions:
        _save_records(records)
        log.info("Wallet tracker: logged %d newly-observed position(s).", len(new_positions))

    return new_positions


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

        # A winning share always pays out $1, regardless of entry price —
        # so profit per dollar invested is (1/entry_price - 1). A losing
        # position simply loses the full stake. Skip the P&L calc (leave
        # it None) if we never captured a valid entry price for this
        # record — better to omit than compute a nonsense number.
        size_usd = r.get("size_usd")
        entry_price = r.get("entry_price")
        if size_usd and entry_price and entry_price > 0:
            if r["correct"]:
                r["realized_pnl_usd"] = size_usd * (1 / entry_price - 1)
            else:
                r["realized_pnl_usd"] = -size_usd

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
    """Short display string for a Telegram message, e.g. '12-4, 75%'
    (confident sample), '1-0, 100% (early)' (below MIN_SAMPLE_SIZE — real
    number, just flagged as not yet statistically meaningful), or 'new'
    (zero resolved — genuinely nothing to show yet)."""
    rec = get_wallet_category_record(wallet, category)
    if rec["total_resolved"] == 0:
        return "new"
    if rec["total_resolved"] < MIN_SAMPLE_SIZE:
        return f"{rec['wins']}-{rec['losses']}, {rec['win_rate_pct']}% (early)"
    return f"{rec['wins']}-{rec['losses']}, {rec['win_rate_pct']}%"


def get_recent_form(wallet: str, category: str, n: int = 5) -> dict:
    """Win/loss over just the wallet's LAST n resolved picks in this
    category — not their whole career. A strong career record can hide a
    current cold streak (or hide a current hot streak the career number
    hasn't caught up to yet). Returns {wins, total}."""
    records = _load_records()
    resolved = [
        r for r in records.values()
        if r["wallet"] == wallet and r["category"] == category
        and r["status"] == "closed" and r.get("resolved_at")
    ]
    resolved.sort(key=lambda r: r["resolved_at"], reverse=True)
    recent = resolved[:n]
    wins = sum(1 for r in recent if r["correct"])
    return {"wins": wins, "total": len(recent)}


def format_wallet_record_with_recent(wallet: str, category: str, n: int = 5) -> str:
    """Same as format_wallet_record, but appends recent form too — used
    specifically for Elite Mover alerts, where a single trader's CURRENT
    form matters more than in a multi-trader consensus message. Only
    appends if there's at least 2 recent picks to speak of (otherwise
    "recent 1: 1-0" adds noise, not signal)."""
    base = format_wallet_record(wallet, category)
    recent = get_recent_form(wallet, category, n=n)
    if recent["total"] >= 2:
        base += f" (recent {recent['total']}: {recent['wins']}-{recent['total'] - recent['wins']})"
    return base


def get_portfolio_concentration(wallet: str, position_size_usd: float) -> dict:
    """What % of a wallet's total CURRENTLY OPEN tracked portfolio this
    one position represents. The same $5,000 bet means something very
    different from someone with a $14,700 total open book versus someone
    with a $200,000 one — this is what actually distinguishes "a big
    swing for them" from "just another position." Returns
    {total_open_usd, concentration_pct} — concentration_pct is None if
    we can't compute a meaningful total (e.g. total is 0)."""
    records = _load_records()
    total_open = sum(
        r["size_usd"] for r in records.values()
        if r["wallet"] == wallet and r["status"] == "open"
    )
    if total_open <= 0:
        return {"total_open_usd": 0.0, "concentration_pct": None}
    concentration_pct = round(100 * position_size_usd / total_open, 1)
    return {"total_open_usd": round(total_open, 2), "concentration_pct": concentration_pct}


MIN_CONCENTRATION_BASELINE_SAMPLE = 3  # need at least this many prior bets before trusting "their usual %"


def get_average_concentration(wallet: str, exclude_market_id: str = None, exclude_outcome: str = None) -> dict:
    """A wallet's average portfolio-concentration-at-entry across their
    OTHER recorded positions (open and closed) — their personal baseline
    betting pattern. This is what actually answers "is 34% unusual for
    THEM," rather than just reporting 34% in isolation, which can't
    distinguish a disciplined trader making a rare high-conviction call
    from someone who bets big every single time as a matter of course.
    Excludes the position currently being evaluated (if given), so it
    doesn't drag its own baseline toward itself. Returns {avg_pct,
    sample_count} — avg_pct is None if there's no usable prior history
    yet (either genuinely new to us, or all their history predates this
    field being tracked — see record_observed_positions)."""
    records = _load_records()
    values = [
        r["concentration_pct_at_entry"] for r in records.values()
        if r["wallet"] == wallet
        and r.get("concentration_pct_at_entry") is not None
        and not (r["market_id"] == exclude_market_id and r["outcome"] == exclude_outcome)
    ]
    if not values:
        return {"avg_pct": None, "sample_count": 0}
    return {"avg_pct": round(sum(values) / len(values), 1), "sample_count": len(values)}


def format_conviction_line(wallet: str, position_size_usd: float, market_id: str, outcome: str) -> str:
    """The full conviction picture: this bet's concentration, AND
    whether that's unusual for this specific wallet or just how they
    always operate. Returns None if we can't compute a concentration at
    all (no open-portfolio data)."""
    concentration = get_portfolio_concentration(wallet, position_size_usd)
    if concentration["concentration_pct"] is None:
        return None

    baseline = get_average_concentration(wallet, exclude_market_id=market_id, exclude_outcome=outcome)
    if baseline["avg_pct"] is not None and baseline["sample_count"] >= MIN_CONCENTRATION_BASELINE_SAMPLE:
        comparison = f" — vs their usual ~{baseline['avg_pct']}% ({baseline['sample_count']} prior bets)"
    else:
        comparison = " — no baseline yet to compare against"

    return (f"{concentration['concentration_pct']}% of tracked portfolio "
            f"(${concentration['total_open_usd']:,.0f} total open){comparison}")


def format_wallet_roi(wallet: str) -> str:
    """Short display string for a wallet's OVERALL realized ROI% (every
    category combined — this is the same number driving the pool
    reranking), e.g. '+142.6% (12 resolved)' or '-38.0% (1 resolved)'.
    'no data yet' if the wallet has zero resolved positions at all.
    Unlike the win-rate display, this is never gated by sample size — see
    get_wallet_realized_roi for why."""
    roi = get_wallet_realized_roi(wallet)
    if roi["resolved_count"] == 0:
        return "no data yet"
    sign = "+" if roi["roi_pct"] >= 0 else ""
    return f"{sign}{roi['roi_pct']}% ({roi['resolved_count']} resolved)"


def _format_age(iso_timestamp: str) -> str:
    """e.g. '2h ago', '3d ago' — same style as trade_tracker's version,
    duplicated here rather than shared since it's a tiny, self-contained
    helper (consistent with this codebase's existing per-module pattern)."""
    if not iso_timestamp:
        return "unknown"
    try:
        seen = datetime.fromisoformat(iso_timestamp)
    except (ValueError, TypeError):
        return "unknown"
    delta = datetime.now(timezone.utc) - seen
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{int(delta.total_seconds() / 60)}m ago"
    if hours < 24:
        return f"{int(hours)}h ago"
    return f"{int(hours / 24)}d ago"


def get_position_ages(wallets: list[str], market_id: str, outcome: str) -> dict:
    """For each wallet, how long ago THEY specifically entered this exact
    position — not when the signal fired, when they actually got in.
    Four traders who all entered within the same hour is a very different
    signal than four who each got in independently over three weeks.
    Returns {wallet: age_str}, 'unknown' for anything we don't have a
    first_seen_at for (e.g. positions logged before this field existed)."""
    records = _load_records()
    ages = {}
    for w in wallets:
        key = _record_key(w, market_id, outcome)
        rec = records.get(key)
        ages[w] = _format_age(rec["first_seen_at"]) if rec else "unknown"
    return ages


def get_price_changes(wallets: list[str], market_id: str, outcome: str, current_price: float) -> dict:
    """For each wallet, how many percentage points the price has moved
    since THEY specifically entered — positive means the price has moved
    TOWARD this outcome since they got in (their read is being confirmed
    by the market), negative means it's drifted away (their read is being
    challenged). Uses entry_price, already captured per-position for the
    ROI feature — no new tracking needed. Returns {wallet: change_str},
    e.g. '+15pts' or '-5pts', 'unknown' if we lack a valid entry price or
    current price to compare against."""
    records = _load_records()
    changes = {}
    for w in wallets:
        key = _record_key(w, market_id, outcome)
        rec = records.get(key)
        entry_price = rec.get("entry_price") if rec else None
        if not entry_price or entry_price <= 0 or not current_price:
            changes[w] = "unknown"
            continue
        delta_pts = round((current_price - entry_price) * 100)
        sign = "+" if delta_pts >= 0 else ""
        changes[w] = f"{sign}{delta_pts}pts"
    return changes


def get_wallet_realized_roi(wallet: str) -> dict:
    """Dollar-weighted realized ROI% across ALL of a wallet's resolved
    positions, in every category combined — not a naive average of each
    position's own percent return, which would let one tiny lucky bet
    dominate the number. A wallet that turned $200 into $300 (50% ROI)
    ranks above one that turned $50,000 into $52,000 (4% ROI), even
    though the second made more raw dollars.

    Returns {total_invested, total_pnl_usd, roi_pct, resolved_count}.
    roi_pct is available the moment a wallet has ANY resolved position
    (resolved_count >= 1) — deliberately NOT gated behind MIN_SAMPLE_SIZE
    the way the win-rate display is. That means a single resolved bet can
    swing a wallet from last to first in the ranking; this is an accepted
    tradeoff, not an oversight — real numbers immediately, accepting more
    noise early on, rather than withholding the number until confident.
    """
    records = _load_records()
    resolved = [
        r for r in records.values()
        if r["wallet"] == wallet and r["status"] == "closed"
        and r.get("realized_pnl_usd") is not None
    ]

    total_invested = sum(r["size_usd"] for r in resolved)
    total_pnl = sum(r["realized_pnl_usd"] for r in resolved)
    resolved_count = len(resolved)

    roi_pct = None
    if resolved_count >= 1 and total_invested > 0:
        roi_pct = round(100 * total_pnl / total_invested, 1)

    return {
        "total_invested": round(total_invested, 2),
        "total_pnl_usd": round(total_pnl, 2),
        "roi_pct": roi_pct,
        "resolved_count": resolved_count,
    }


def rank_wallets_by_realized_roi(wallets: list[str]) -> list[str]:
    """Reorders a candidate pool by historical realized ROI% instead of
    Polymarket's own profit-rank ordering. Any wallet with at least ONE
    resolved position is ranked by ROI% descending; wallets with zero
    resolved positions (nothing at all to go on yet) keep their original
    relative order and are appended after. Since ROI% is available from a
    single resolved bet, early rankings can swing a lot on small samples —
    that's accepted, not a bug (see get_wallet_realized_roi)."""
    confident = []
    unproven = []
    for w in wallets:
        roi = get_wallet_realized_roi(w)
        if roi["roi_pct"] is not None:
            confident.append((w, roi["roi_pct"]))
        else:
            unproven.append(w)

    confident.sort(key=lambda pair: pair[1], reverse=True)
    return [w for w, _ in confident] + unproven
