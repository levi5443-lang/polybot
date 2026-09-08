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
  3. Set these environment variables (NEVER commit them, NEVER hardcode
     them — set them in Render's dashboard as secret env vars):
       POLYMARKET_PRIVATE_KEY   - the wallet's private key
       POLYMARKET_WALLET_ADDRESS - your Polymarket PROFILE address (the
         proxy wallet that actually holds your funds/positions — check
         this against the address your signing wallet itself reports; on
         many accounts (browser-wallet / WalletConnect logins) these are
         two different addresses, and it's the profile/proxy one that
         belongs here)
  4. The wallet needs USDC trading approval set on Polymarket's exchange
     contracts — this typically happens automatically the first time you
     interact with Polymarket's UI using that wallet; verify this manually
     before relying on the bot to trade.

Signature type: this file assumes your account trades through a proxy
contract (signature_type=2 — the common case when you connected a wallet
via WalletConnect/browser-wallet rather than trading as a raw EOA). If your
Polymarket profile address is identical to your signing wallet's own
address, you're trading as a plain EOA instead — change signature_type to
0 and drop the funder argument in _get_client() below.
UNVERIFIED against a live account — confirm against current py-clob-client
docs/examples before trusting this with real size.
"""

import os
import logging

log = logging.getLogger("execution")

CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137


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

    funder = os.environ.get("POLYMARKET_WALLET_ADDRESS")
    if not funder:
        raise RuntimeError(
            "POLYMARKET_WALLET_ADDRESS environment variable is not set. "
            "This must be your Polymarket PROFILE address (the proxy that "
            "holds your funds) — not your raw wallet's own address, if "
            "those two differ for your account."
        )

    # signature_type=2: browser-wallet proxy (Polymarket deployed a proxy
    # contract for this account, separate from the EOA behind the private
    # key). If your account instead trades directly as an EOA (Polymarket
    # profile address == wallet address), use signature_type=0 and drop
    # `funder` instead. UNVERIFIED against a live account — confirm against
    # current py-clob-client docs before trusting this with real size.
    client = ClobClient(
        CLOB_HOST,
        key=private_key,
        chain_id=POLYGON_CHAIN_ID,
        signature_type=2,
        funder=funder,
    )
    # py-clob-client requires deriving/setting API credentials once per key.
    # NOTE: verify this call still matches the current py-clob-client
    # version's interface before relying on it — client libraries change.
    client.set_api_creds(client.create_or_derive_api_creds())
    return client


def get_wallet_balance_usd() -> float:
    """Current USDC collateral balance available to trade with.

    NOTE: unverified — py-clob-client's balance-check method/response shape
    should be confirmed against current docs before trusting this number.
    """
    client = _get_client()
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    resp = client.get_balance_allowance(params)
    # Response is typically in the smallest USDC unit (6 decimals) — verify
    # this against a real response before trusting the /1e6 conversion.
    raw_balance = resp.get("balance") if isinstance(resp, dict) else getattr(resp, "balance", 0)
    return float(raw_balance) / 1_000_000


def place_market_buy(token_id: str, size_usd: float) -> dict:
    """Place a market buy order for approximately size_usd worth of the
    given outcome token.

    NOTE: unverified — order construction (OrderArgs field names, market
    vs. limit order helper method names) should be confirmed against the
    current py-clob-client docs/examples before relying on this in
    production. This is written against the commonly-documented pattern
    but client libraries change their exact interfaces over time.
    """
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
