"""
trade_optimizer.py — read-only sensitivity analysis over the bot's own
resolved paper-trade history.

This is Tier 1 of "can the bot learn over time" (2026-10-15, Levi's
request). It does NOT change any trading behavior, threshold, or config
value by itself — it only reads risk_manager's trade ledger (enriched
2026-10-13 with agreement_count/avg_wallet_roi_pct per trade) and reports
back what the DATA says about whether current settings are well
calibrated. A human (Levi) decides whether to act on it; nothing here
writes back to category_leaderboard.py/early_movers.py/wallet_tracker.py
or any other config constant.

WHY NOT FULLY AUTOMATIC: with a still-thin resolved sample, and
prediction-market trader behavior that can shift over time, an
auto-tuning loop risks fitting to noise rather than real edge, and would
mean real-money-adjacent parameters changing without anyone reviewing
why. This module is deliberately the "surface the evidence, human
decides" half of that — the same pattern already used everywhere else in
this codebase (nothing changes config without Levi's explicit go-ahead),
extended here to automated analysis too.

IMPORTANT LIMITATION: this can only show what happens if you had traded
MORE SELECTIVELY than your actual live settings (e.g. "if the consensus
threshold had been 6 instead of 4") — it can never show what a LOOSER
setting would have done, because trades below the current threshold
never fired in the first place and so were never recorded. Every report
below says this explicitly rather than implying otherwise.
"""

import logging

import risk_manager

log = logging.getLogger("trade_optimizer")

MIN_BUCKET_SAMPLE = 5  # don't show/trust a bucket's win rate until it has at least this many
                        # resolved trades — same bar used elsewhere in this codebase
                        # (wallet_tracker.MIN_SAMPLE_SIZE) before treating a win rate as real
                        # signal instead of small-sample noise


def _resolved_paper_trades() -> list[dict]:
    trades = risk_manager.get_all_trades()
    return [
        t for t in trades
        if t.get("mode") == "paper" and t.get("status") == "closed"
        and t.get("realized_pnl") is not None
    ]


def _bucket_stats(trades: list[dict]) -> dict:
    """wins/total/win_rate_pct/avg_pnl for one bucket of trades. avg_pnl
    is per-$1-unit (paper trades are logged at a fixed $1 notional), so
    it's directly comparable across buckets and signal types."""
    total = len(trades)
    if total == 0:
        return {"total": 0, "wins": 0, "win_rate_pct": None, "avg_pnl": None}
    wins = sum(1 for t in trades if t["realized_pnl"] > 0)
    avg_pnl = sum(t["realized_pnl"] for t in trades) / total
    return {
        "total": total,
        "wins": wins,
        "win_rate_pct": round(100 * wins / total, 1),
        "avg_pnl": round(avg_pnl, 4),
    }


def analyze_by_signal_type() -> dict:
    """{signal_type: stats} across every resolved paper trade."""
    trades = _resolved_paper_trades()
    by_type: dict[str, list] = {}
    for t in trades:
        by_type.setdefault(t.get("signal_type", "unknown"), []).append(t)
    return {k: _bucket_stats(v) for k, v in by_type.items()}


def analyze_consensus_agreement_cutoffs(min_cutoff: int = 4, max_cutoff: int = 8) -> dict:
    """For Consensus trades only: at each possible 'what if the threshold
    had been >= N' cutoff from min_cutoff to max_cutoff, what would win
    rate/avg P&L have looked like among trades that would STILL have
    fired? Can only raise the bar above the real threshold (4) — see
    module docstring for why it can't simulate going lower. Returns
    {cutoff: stats}."""
    trades = [t for t in _resolved_paper_trades() if t.get("signal_type") == "consensus"]
    results = {}
    for cutoff in range(min_cutoff, max_cutoff + 1):
        subset = [t for t in trades if (t.get("agreement_count") or 0) >= cutoff]
        results[cutoff] = _bucket_stats(subset)
    return results


def analyze_price_band(bucket_width: float = 0.2) -> dict:
    """Buckets ALL resolved paper trades (every signal type) by entry
    price into bucket_width-wide bands (e.g. 0.0-0.2, 0.2-0.4, ...),
    showing win rate/avg P&L per band — a direct empirical check on
    whether the current price-room filters (15-85% for Consensus/Early
    Movers) are set at a sensible boundary, or whether the data suggests
    moving them. Returns {(band_start, band_end): stats}."""
    trades = _resolved_paper_trades()
    results = {}
    band_start = 0.0
    while band_start < 1.0:
        band_end = round(band_start + bucket_width, 2)
        subset = [
            t for t in trades
            if t.get("entry_price") is not None and band_start <= t["entry_price"] < band_end
        ]
        results[(band_start, band_end)] = _bucket_stats(subset)
        band_start = band_end
    return results


def format_optimization_report() -> str:
    """Telegram-ready text report — the /optimize command's output.
    Every breakdown respects MIN_BUCKET_SAMPLE; buckets below it are
    either hidden or flagged as not-yet-meaningful rather than shown as
    if they were a real finding."""
    all_resolved = _resolved_paper_trades()
    if len(all_resolved) < MIN_BUCKET_SAMPLE:
        return (f"📊 *Optimization report*\n\nOnly {len(all_resolved)} resolved paper trade(s) "
                f"so far — need at least {MIN_BUCKET_SAMPLE} before any breakdown here is "
                f"meaningful rather than noise. Check back once more have resolved.")

    lines = ["📊 *Optimization report* (read-only — nothing here changes automatically)\n"]

    lines.append("*By signal type:*")
    by_type = analyze_by_signal_type()
    for signal_type, stats in sorted(by_type.items(), key=lambda kv: -kv[1]["total"]):
        if stats["total"] < MIN_BUCKET_SAMPLE:
            lines.append(f"  {signal_type}: only {stats['total']} resolved — not enough yet")
        else:
            lines.append(f"  {signal_type}: {stats['wins']}/{stats['total']} ({stats['win_rate_pct']}%), "
                          f"avg P&L ${stats['avg_pnl']:+.2f}/unit")

    consensus_cutoffs = analyze_consensus_agreement_cutoffs()
    shown_any_cutoff = any(v["total"] >= MIN_BUCKET_SAMPLE for v in consensus_cutoffs.values())
    if shown_any_cutoff:
        lines.append("\n*Consensus — if the agreement threshold had been higher:*")
        lines.append("  (can only simulate RAISING the bar above the real 4-of-10 "
                      "threshold — lower settings were never traded, so there's no data for them)")
        for cutoff, stats in sorted(consensus_cutoffs.items()):
            if stats["total"] >= MIN_BUCKET_SAMPLE:
                lines.append(f"  >={cutoff}: {stats['wins']}/{stats['total']} ({stats['win_rate_pct']}%), "
                              f"avg P&L ${stats['avg_pnl']:+.2f}/unit")

    price_bands = analyze_price_band()
    shown_any_band = any(v["total"] >= MIN_BUCKET_SAMPLE for v in price_bands.values())
    if shown_any_band:
        lines.append("\n*By entry price band (all signal types):*")
        for (lo, hi), stats in sorted(price_bands.items()):
            if stats["total"] >= MIN_BUCKET_SAMPLE:
                lines.append(f"  {lo:.0%}-{hi:.0%}: {stats['wins']}/{stats['total']} ({stats['win_rate_pct']}%), "
                              f"avg P&L ${stats['avg_pnl']:+.2f}/unit")

    if not shown_any_cutoff and not shown_any_band:
        lines.append("\nNot enough resolved trades in any one bucket yet for the deeper "
                      "breakdowns — check back as more resolve.")

    return "\n".join(lines)
