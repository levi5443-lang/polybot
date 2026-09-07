"""
signal_momentum.py — tracks how a signal's agreement count changes across
consecutive polling cycles, so alerts can show whether conviction is
BUILDING (2 -> 3 -> 4 tracked traders) or just sitting STEADY, rather
than always looking identical regardless of trajectory.

Only meaningful for regular category consensus signals, which persist and
get re-checked across many cycles — early movers and elite moves are
single-moment-in-time events by design, so momentum doesn't apply there.
"""

from shared_storage import get_json, set_json

MOMENTUM_KEY = "signal_momentum.json"
MAX_HISTORY = 6  # ~30 min of history at the 5-min poll interval


def _signal_key(market_id: str, outcome: str) -> str:
    return f"{market_id}|{outcome}"


def record_and_get_momentum(market_id: str, outcome: str, current_count: int) -> dict:
    """Records this cycle's agreement count and returns momentum info:
    {trend: 'new'/'growing'/'shrinking'/'steady', history: [2, 3, 4]}.
    Call this once per cycle for every (market_id, outcome) pair with ANY
    tracked agreement — not just ones crossing the alert threshold — so
    real history has already built up by the time a signal first becomes
    alert-worthy. See category_leaderboard.compute_category_consensus,
    which does exactly this as a side effect every cycle."""
    state = get_json(MOMENTUM_KEY, {})
    key = _signal_key(market_id, outcome)
    history = state.get(key, [])
    history.append(current_count)
    history = history[-MAX_HISTORY:]
    state[key] = history
    set_json(MOMENTUM_KEY, state)
    return _trend_from_history(history)


def get_momentum(market_id: str, outcome: str) -> dict:
    """Read-only lookup — does NOT record a new data point. Use this when
    building an alert message for a signal whose momentum was already
    recorded earlier in the same cycle (avoids double-counting)."""
    state = get_json(MOMENTUM_KEY, {})
    history = state.get(_signal_key(market_id, outcome), [])
    return _trend_from_history(history)


def _trend_from_history(history: list) -> dict:
    if len(history) < 2:
        trend = "new"
    elif history[-1] > history[-2]:
        trend = "growing"
    elif history[-1] < history[-2]:
        trend = "shrinking"
    else:
        trend = "steady"
    return {"trend": trend, "history": history}


def format_momentum(momentum: dict) -> str:
    """e.g. 'growing (2→3→4)', 'steady at 4 (last 3 checks)', or '' for a
    brand-new signal with nothing to compare against yet."""
    trend = momentum["trend"]
    history = momentum["history"]

    if trend == "new":
        return ""
    if trend == "steady":
        return f"steady at {history[-1]} (last {len(history)} checks)"
    arrow_path = "→".join(str(h) for h in history)
    return f"{trend} ({arrow_path})"
