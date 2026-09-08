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

Signature type / account model (rewritten 2026-09-08, again, after the
"plain EOA" assumption below turned out to be wrong for this account):

This now AUTO-DETECTS which kind of account it's dealing with, instead of
hardcoding one assumption that kept not matching reality:

  - If POLYMARKET_WALLET_ADDRESS is unset, or equals the address
    POLYMARKET_PRIVATE_KEY itself resolves to: plain EOA account
    (signature_type=0). The key trades directly as its own address, no
    funder needed.

  - If POLYMARKET_WALLET_ADDRESS is set AND differs from the key's own
    address: proxy/smart-contract wallet account (signature_type=2,
    funder=POLYMARKET_WALLET_ADDRESS). The private key SIGNS orders, but
    the actual funds and trades live at the separate funder address.

Why this matters: "connected to Polymarket via Telegram" (Levi's account)
describes Polymarket's email/social-login onboarding, which deploys
exactly this kind of proxy wallet behind the scenes — a signing key at one
address, real funds at a different address, by design. Earlier today this
file assumed a plain EOA (because Levi doesn't have a traditional
polymarket.com/browser-wallet account) and treated the key resolving to a
DIFFERENT address than his funded wallet as a broken/mismatched key. It
wasn't broken — the key was correct the whole time. It's a proxy account,
and the two addresses are SUPPOSED to differ. No further key changes
should be needed once POLYMARKET_WALLET_ADDRESS is set correctly in
Render — see get_wallet_status() below for the self-check.

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


def _resolve_account_config():
    """Figure out whether this is a plain EOA or a proxy/smart-contract
    wallet account, and which address actually holds funds and receives
    orders. See the module docstring above for the full reasoning.

    Returns (private_key, signer_address, trading_address, signature_type).
    Raises RuntimeError if POLYMARKET_PRIVATE_KEY is missing or malformed.
    """
    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not private_key:
        raise RuntimeError(
            "POLYMARKET_PRIVATE_KEY environment variable is not set. "
            "Real execution cannot proceed without it."
        )

    try:
        from eth_account import Account
        signer_address = Account.from_key(private_key).address
    except Exception as e:
        raise RuntimeError(
            f"POLYMARKET_PRIVATE_KEY is malformed — could not derive an "
            f"address from it: {e}"
        )

    funder_address = os.environ.get("POLYMARKET_WALLET_ADDRESS")
    if funder_address and funder_address.strip().lower() != signer_address.strip().lower():
        log.info(
            "Proxy/smart-contract wallet mode: signing key %s controls "
            "trading account %s (from POLYMARKET_WALLET_ADDRESS). This is "
            "normal for an account created via email/Telegram/social login "
            "rather than a browser wallet — the two addresses are supposed "
            "to differ.",
            signer_address, funder_address,
        )
        return private_key, signer_address, funder_address, 2

    log.info("Plain EOA mode: trading directly as %s.", signer_address)
    return private_key, signer_address, signer_address, 0


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

    private_key, signer_address, trading_address, signature_type = _resolve_account_config()

    if signature_type == 2:
        client = ClobClient(
            CLOB_HOST,
            key=private_key,
            chain_id=POLYGON_CHAIN_ID,
            signature_type=2,
            funder=trading_address,
        )
    else:
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
    client._signer_address = signer_address    # stashed for get_wallet_status() below
    client._trading_address = trading_address
    return client


def get_wallet_status() -> dict:
    """Read-only sanity check — never writes anything, never touches Render.

    Returns:
        {"signer_address": <address POLYMARKET_PRIVATE_KEY resolves to>,
         "trading_address": <address actually used for balance/orders —
                              same as signer_address in EOA mode, or
                              POLYMARKET_WALLET_ADDRESS in proxy mode>,
         "mode": "proxy" | "eoa" | "error",
         "error": <set only when mode == "error">}
    """
    try:
        _, signer_address, trading_address, signature_type = _resolve_account_config()
    except Exception as e:
        return {"signer_address": None, "trading_address": None, "mode": "error", "error": str(e)}
    return {
        "signer_address": signer_address,
        "trading_address": trading_address,
        "mode": "proxy" if signature_type == 2 else "eoa",
    }


def get_wallet_address() -> str:
    """Back-compat helper: returns the address actually used for
    balance/orders (see get_wallet_status), or an 'unknown (...)' message
    if it couldn't be determined."""
    status = get_wallet_status()
    if status["mode"] == "error":
        return f"unknown ({status['error']})"
    return status["trading_address"]


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
