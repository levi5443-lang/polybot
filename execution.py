"""
execution.py — places real orders on Polymarket via py-clob-client-v2.

⚠️ UNVERIFIED AGAINST A LIVE WALLET. Everything else in this project has
been tested against real Polymarket data. This file has not — there is no
funded wallet to test it with, and this environment has no network access
to Polymarket's order-placement endpoints at all. Treat this as a first
draft written against py-clob-client-v2's actual installed source (read
directly, not guessed from docs — see below), not as proven-working code.
Test with the smallest possible real amount before trusting it with
anything larger.

LIBRARY VERSION (fixed 2026-09-08 — this was the actual root cause of the
"$0.00 balance" that survived every previous fix): Polymarket did a full
exchange upgrade on 2026-04-28 (new CTF Exchange V2, and a new native
collateral token, pUSD, replacing USDC.e). Their own migration notes say
plainly: "v1 client libraries won't work with the new contracts." This
project was still on the old `py-clob-client` package. It wasn't throwing
errors — it was reaching a real endpoint and getting a real, well-formed
200 OK response — it was just asking about the OLD USDC.e collateral
pool, which is genuinely empty now that everything is pUSD. That's why
every wallet-address fix still read $0.00: the wallet was right, the
signing was right, the balance was right — for a token that isn't used
anymore. Confirmed by installing py-clob-client-v2 1.1.0 in a throwaway
venv and reading its actual client.py/clob_types.py source rather than
trusting scraped docs (which gave conflicting, some hallucinated-looking,
answers). Relevant facts confirmed directly from that source:
  - ClobClient's constructor signature is UNCHANGED: still
    ClobClient(host, chain_id, key=..., creds=..., signature_type=...,
    funder=...). No changes needed there.
  - client.create_or_derive_api_creds() was RENAMED to
    client.create_or_derive_api_key() — v2 has no create_or_derive_api_creds
    at all, so the old call would fail once the right package is installed.
  - AssetType.COLLATERAL still means "whatever the current collateral
    token is" (now pUSD) — no separate pUSD-specific enum value needed.
  - MarketOrderArgs is still a valid import (an alias for the new
    MarketOrderArgsV2), and order_builder.constants.BUY is unchanged.
  - client.create_and_post_market_order(order_args) is the recommended
    single call for a market order now (builds + posts + retries once on
    a version mismatch) — used below instead of manual
    create_market_order() + post_order().

Setup required before this can do anything:
  1. pip install py-clob-client-v2   (NOT py-clob-client — see above)
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
    """Build an authenticated CLOB client. Imports py-clob-client-v2 lazily
    so the rest of the bot works fine even if that package isn't installed
    — it's only needed once PAPER_MODE is actually turned off."""
    try:
        from py_clob_client_v2.client import ClobClient
    except ImportError:
        raise RuntimeError(
            "py-clob-client-v2 isn't installed. Run: pip install py-clob-client-v2 "
            "(NOT py-clob-client — see the module docstring for why)."
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
    # py-clob-client-v2 requires deriving/setting API credentials once per
    # key. Method name confirmed directly from the installed v2 source:
    # create_or_derive_api_key() — the old create_or_derive_api_creds() no
    # longer exists in this package.
    client.set_api_creds(client.create_or_derive_api_key())
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
    """Current collateral (pUSD) balance available to trade with.

    Wrapped in _with_timeout — see that function's docstring for why.
    """
    def _do():
        client = _get_client()
        from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType

        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        resp = client.get_balance_allowance(params)
        # Log the RAW response every time, before any conversion — added
        # 2026-09-08 because a $0.00 result was previously indistinguishable
        # from "Polymarket genuinely says zero" vs. "the response didn't
        # have the shape we expected and we silently defaulted to 0" (the
        # old code's getattr(resp, "balance", 0) fallback could mask a real
        # problem behind a fake-looking zero). This makes the actual
        # Polymarket response visible in the logs every single time.
        log.info("Raw balance-allowance response for trading_address=%s: %r",
                  getattr(client, "_trading_address", "unknown"), resp)
        # Response is typically in the smallest USDC unit (6 decimals) —
        # verify this against a real response before trusting the /1e6
        # conversion.
        raw_balance = resp.get("balance") if isinstance(resp, dict) else getattr(resp, "balance", 0)
        return float(raw_balance) / 1_000_000

    return _with_timeout(_do)


def place_market_buy(token_id: str, size_usd: float) -> dict:
    """Place a market buy order for approximately size_usd worth of the
    given outcome token.

    Uses create_and_post_market_order() — confirmed directly from the
    installed py-clob-client-v2 source to be the single recommended call
    for this (it builds the order, posts it as FOK by default, and retries
    once automatically if the exchange reports a version mismatch), rather
    than the old manual create_market_order() + post_order() two-step.
    Wrapped in _with_timeout — see that function's docstring for why.
    """
    def _do():
        client = _get_client()
        from py_clob_client_v2.clob_types import MarketOrderArgs
        from py_clob_client_v2.order_builder.constants import BUY

        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=size_usd,
            side=BUY,
        )
        resp = client.create_and_post_market_order(order_args)
        log.info("Order submitted: token=%s size=$%.2f response=%s", token_id, size_usd, resp)
        return resp

    return _with_timeout(_do)
