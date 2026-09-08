"""
execution.py — places real orders on Polymarket via py-clob-client.

⚠️ UNVERIFIED AGAINST A LIVE WALLET. Everything else in this project has
been tested against real Polymarket data. This file has not — there is no
funded wallet to test it with, and this environment has no network access
to Polymarket's order-placement endpoints at all. Treat this as a first
draft written against py-clob-client's documented interface, not as
proven-working code. Test with the smallest possible real amount before
trusting it with anything larger.

Setup required before this can do anything:
  1. pip install py-clob-client
  2. A Polygon wallet, funded with USDC (or pUSD), private key available
  3. Set this environment variable (NEVER commit it, NEVER hardcode it —
     set it in Render's dashboard as a secret env var):
       POLYMARKET_PRIVATE_KEY   - the wallet's private key
  4. The wallet needs USDC trading approval set on Polymarket's exchange
     contracts — this typically happens automatically the first time you
     interact with Polymarket using that wallet; verify this manually
     before relying on the bot to trade.

Signature type / account model (changed 2026-09-08, Levi's request): this
account trades as a plain EOA — a Telegram-connected wallet used directly
against Polymarket, with no separate polymarket.com profile/account and no
proxy contract sitting in front of it. That's signature_type=0, no funder
address needed — py-clob-client derives everything it needs from the
private key itself.

This file used to assume the OTHER common setup (signature_type=2, a
separate proxy contract address from a browser-wallet/WalletConnect login,
configured via a POLYMARKET_WALLET_ADDRESS env var) — that was the actual
cause of every live trade reading a $0.00 balance and refusing to trade:
the bot kept asking Polymarket about a proxy relationship that doesn't
exist for this wallet. If this ever changes (e.g. Levi moves to a real
Polymarket account with a browser-wallet login), switch signature_type
back to 2 and reintroduce a funder address — see git history for the
previous version of this function.
UNVERIFIED against a live account — confirm against current py-clob-client
docs/examples before trusting this with real size.
"""

import os
import signal
import logging

log = logging.getLogger("execution")

CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137
NETWORK_TIMEOUT_SECONDS = 20  # see _with_timeout below


class ExecutionTimeout(Exception):
    """Raised when a Polymarket network call takes too long. Caught by
    trade_approval.py's try/except around _execute_approved_trade() the
    same way any other execution error is — surfaces as a Telegram
    message instead of hanging."""


def _with_timeout(fn, *args, **kwargs):
    """Runs fn(*args, **kwargs) but forcibly gives up after
    NETWORK_TIMEOUT_SECONDS instead of hanging forever.

    Added 2026-09-08 (Levi's request) after this entire single-threaded
    bot froze completely — no more Telegram polling, no more market
    scans, nothing — the moment a live trade was approved. The prime
    suspect: py-clob-client's internal HTTP calls (deriving API creds,
    checking balance, placing an order) have no guaranteed timeout of
    their own, and because this whole process is one single thread, ANY
    call that hangs freezes literally everything else the bot does,
    including the fast Telegram-approval loop — which is exactly the
    "nothing happens after I approve" symptom, just one level deeper
    than the earlier bugs already fixed today.

    Uses SIGALRM (signal-based, main-thread-only, Unix — fine for a
    Render worker) rather than a background thread, since py-clob-client
    calls aren't necessarily safe to abandon mid-flight from another
    thread while still holding network sockets open.
    """
    def _on_timeout(signum, frame):
        raise ExecutionTimeout(
            f"Polymarket call took longer than {NETWORK_TIMEOUT_SECONDS}s and was aborted."
        )

    previous_handler = signal.signal(signal.SIGALRM, _on_timeout)
    signal.alarm(NETWORK_TIMEOUT_SECONDS)
    try:
        return fn(*args, **kwargs)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)


def _get_client():
    """Build an authenticated CLOB client. Imports py-clob-client lazily so
    the rest of the bot works fine even if that package isn't installed —
    it's only needed once PAPER_MODE is actually turned off."""
    try:
        from py_clob_client.client import ClobClient
    except ImportError:
        raise RuntimeError(
            "py-clob-client isn't installed. Run: pip install py-clob-client"
        )

    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not private_key:
        raise RuntimeError(
            "POLYMARKET_PRIVATE_KEY environment variable is not set. "
            "Real execution cannot proceed without it."
        )

    # Log which address this key actually resolves to. A wrong or
    # mismatched key never errors here — eth_account happily derives SOME
    # valid address from any well-formed hex key, it just won't be the
    # funded wallet's address, and the bot would go on to correctly (and
    # silently) report a real $0.00 balance for that other wallet. This
    # line is the one place that lets Levi directly compare "the address
    # the bot is actually using" against his real funded wallet, instead
    # of guessing whether a newly-pasted key took effect. (2026-09-08)
    try:
        from eth_account import Account
        derived_address = Account.from_key(private_key).address
        log.info("POLYMARKET_PRIVATE_KEY resolves to wallet address: %s", derived_address)
    except Exception as e:
        derived_address = None
        log.warning("Could not derive an address from POLYMARKET_PRIVATE_KEY "
                    "to sanity-check it (this may indicate a malformed key): %s", e)

    # If Levi has also set POLYMARKET_WALLET_ADDRESS (his known-funded
    # wallet), cross-check it against what the key actually resolves to —
    # purely a read of the environment, never a write to it. Loud on
    # mismatch so this is impossible to miss in the logs. (2026-09-08)
    expected_address = os.environ.get("POLYMARKET_WALLET_ADDRESS")
    if expected_address and derived_address:
        if expected_address.strip().lower() != derived_address.strip().lower():
            log.error(
                "WALLET MISMATCH: POLYMARKET_PRIVATE_KEY resolves to %s but "
                "POLYMARKET_WALLET_ADDRESS says the funded wallet is %s. "
                "This key does NOT control the funded wallet — trades will "
                "keep reading a $0.00 balance until the key in Render "
                "actually matches this address.",
                derived_address, expected_address,
            )
        else:
            log.info("Wallet check OK: POLYMARKET_PRIVATE_KEY matches "
                      "POLYMARKET_WALLET_ADDRESS (%s).", expected_address)

    # signature_type=0: plain EOA — trades directly as the wallet behind
    # POLYMARKET_PRIVATE_KEY, no proxy contract, no funder address. See the
    # module docstring above for why this changed from signature_type=2.
    client = ClobClient(
        CLOB_HOST,
        key=private_key,
        chain_id=POLYGON_CHAIN_ID,
        signature_type=0,
    )
    # py-clob-client requires deriving/setting API credentials once per key.
    # NOTE: verify this call still matches the current py-clob-client
    # version's interface before relying on it — client libraries change.
    client.set_api_creds(client.create_or_derive_api_creds())
    client._resolved_address = derived_address  # stashed for get_wallet_address() below
    return client


def get_wallet_address() -> str:
    """Returns the wallet address POLYMARKET_PRIVATE_KEY actually resolves
    to, or 'unknown' if it couldn't be derived. Used to surface this in
    Telegram messages so it's directly visible without checking server
    logs — see the comment in _get_client() above for why this matters."""
    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not private_key:
        return "unknown (POLYMARKET_PRIVATE_KEY not set)"
    try:
        from eth_account import Account
        return Account.from_key(private_key).address
    except Exception:
        return "unknown (couldn't derive from POLYMARKET_PRIVATE_KEY)"


def get_wallet_status() -> dict:
    """Read-only sanity check — never writes anything, never touches Render.

    Added 2026-09-08 after several rounds of Levi manually pasting a new
    POLYMARKET_PRIVATE_KEY into Render and it still not matching his funded
    wallet (0xf0D6...198 kept showing up instead of the expected
    0xeF566a...D07). Rather than me updating Render's env vars — Levi has
    asked that I not touch them — this lets the *env itself* declare what
    it's supposed to be: if he also sets an optional POLYMARKET_WALLET_ADDRESS
    env var to his real funded wallet address, the bot compares it against
    what the private key actually resolves to on every balance/trade check,
    and says plainly whether they match. If POLYMARKET_WALLET_ADDRESS isn't
    set, this just reports the resolved address with no comparison — still
    useful, just not self-checking.

    Returns:
        {"resolved": <address the private key resolves to, or an
                      "unknown (...)" message>,
         "expected": <POLYMARKET_WALLET_ADDRESS value, or None if unset>,
         "mismatch": True  -> both are set and they don't match
                     False -> both are set and they match
                     None  -> nothing to compare (expected unset, or
                              resolved couldn't be derived)}
    """
    resolved = get_wallet_address()
    expected = os.environ.get("POLYMARKET_WALLET_ADDRESS")
    if not expected:
        return {"resolved": resolved, "expected": None, "mismatch": None}
    if not resolved or resolved.startswith("unknown"):
        return {"resolved": resolved, "expected": expected, "mismatch": None}
    mismatch = resolved.strip().lower() != expected.strip().lower()
    return {"resolved": resolved, "expected": expected, "mismatch": mismatch}


def get_wallet_balance_usd() -> float:
    """Current USDC collateral balance available to trade with.

    NOTE: unverified — py-clob-client's balance-check method/response shape
    should be confirmed against current docs before trusting this number.
    Wrapped in _with_timeout — see that function's docstring for why.
    """
    def _do():
        client = _get_client()
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        resp = client.get_balance_allowance(params)
        # Response is typically in the smallest USDC unit (6 decimals) —
        # verify this against a real response before trusting the /1e6
        # conversion.
        raw_balance = resp.get("balance") if isinstance(resp, dict) else getattr(resp, "balance", 0)
        return float(raw_balance) / 1_000_000

    return _with_timeout(_do)


def place_market_buy(token_id: str, size_usd: float) -> dict:
    """Place a market buy order for approximately size_usd worth of the
    given outcome token.

    NOTE: unverified — order construction (OrderArgs field names, market
    vs. limit order helper method names) should be confirmed against the
    current py-clob-client docs/examples before relying on this in
    production. This is written against the commonly-documented pattern
    but client libraries change their exact interfaces over time.
    Wrapped in _with_timeout — see that function's docstring for why.
    """
    def _do():
        client = _get_client()
        from py_clob_client.clob_types import MarketOrderArgs
        from py_clob_client.order_builder.constants import BUY

        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=size_usd,
            side=BUY,
        )
        signed_order = client.create_market_order(order_args)
        resp = client.post_order(signed_order)
        log.info("Order submitted: token=%s size=$%.2f response=%s", token_id, size_usd, resp)
        return resp

    return _with_timeout(_do)
