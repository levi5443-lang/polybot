"""
live_execution.py — the one kill switch gating real Polybot order placement.

Deliberately tiny and deliberately its own file: every place in the
codebase that could submit a real order checks THIS, and only this,
before doing so. Keeping it separate from polybot_client.py (the
mechanics) and polybot_execution.py (the execution backend) means there
is exactly one place to look to answer "can this bot currently place a
real order?" — and exactly one env var to flip to change the answer.

Defaults to OFF. Levi's own words on the plan (2026-10-13): "First we
are perfecting the paper trading, then we will fund the live." This
stays False until he explicitly says to turn it on — consistent with
the standing rule that risk/config changes need his explicit go-ahead,
and this is about as consequential a config flip as exists in this
codebase, since it's the difference between an alert and real money
moving.
"""

import os

ENV_VAR = "POLYBOT_LIVE_EXECUTION_ENABLED"


def is_enabled() -> bool:
    return os.environ.get(ENV_VAR, "").strip().lower() in ("true", "1", "yes")
