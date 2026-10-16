import os
from decimal import Decimal

from dotenv import find_dotenv, load_dotenv

from primedelta import (
    PrimeDelta,
    PriceFeedAddLiquidity,
    PriceFeedRemoveLiquidity,
    SwapSide,
)
from primedelta.settings import DEFAULT_NETWORK

load_dotenv(find_dotenv(".env.local") or find_dotenv(".env"))

primedelta = PrimeDelta(
    private_key=os.environ["PRIMEDELTA_TEST_PRIVATE_KEY"],
    web3_provider_url=os.environ["PRIMEDELTA_PROVIDER_URL"],
    network=os.environ.get("PRIMEDELTA_NETWORK", DEFAULT_NETWORK),
)
primedelta.login()

# TSLA is an oracle stock: the swap below prices against the signed oracle and
# reverts (StalePrice / MarketClosed) outside US market hours. Adding and
# removing liquidity use no price and work at any time; only the buy needs this.
if not primedelta.is_market_open():
    raise SystemExit("market closed — the TSLA buy below needs a signed price")

# Acquire some TSLA so we have stock to pair with the stablecoin for LP.
buy_tx = primedelta.swap_exact_input(
    "TSLA",
    SwapSide.STABLECOIN_TO_STOCK,
    amount_in=Decimal("30"),
    min_amount_out=Decimal("0"),
)
print(f"buy:    {buy_tx}")
tsla_balance = primedelta.get_onchain_stock_balance("TSLA")
print(f"TSLA on chain: {tsla_balance}")

# Add liquidity to the TSLA DCLEX pricefeed pool. The quote says what minting
# 0.001 LP tokens takes now (the pool's own rounding); 1% on top covers swaps
# that move the reserves before the add lands. Size by the dUSD side instead with
# price_feed_add_quote("TSLA", stablecoin_amount=Decimal("10")).
quote = primedelta.price_feed_add_quote("TSLA", liquidity_amount=Decimal(10**15))
liquidity_amount = quote.liquidity_amount

add_tx = primedelta.add_liquidity(
    PriceFeedAddLiquidity(
        symbol="TSLA",
        liquidity_amount=liquidity_amount,
        max_stock_amount=(quote.stock_amount * Decimal("1.01")).quantize(
            Decimal("1e-18")
        ),
        max_stablecoin_amount=(quote.stablecoin_amount * Decimal("1.01")).quantize(
            Decimal("0.000001")
        ),
    )
)
print(f"add:    {add_tx}")

# Read the TSLA-LP holding: lp_balance is in LP tokens, liquidity_amount is the
# same holding in the raw units remove_liquidity takes, and stock_amount /
# stablecoin_amount are what removing all of it pays out now.
position = primedelta.price_feed_lp_position("TSLA")
print(
    f"{position.lp_symbol}: {position.lp_balance} "
    f"(redeems {position.stock_amount} TSLA + {position.stablecoin_amount} dUSD)"
)

# Burn the LP shares back to underlying stock + stablecoin.
remove_tx = primedelta.remove_liquidity(
    PriceFeedRemoveLiquidity(
        symbol="TSLA",
        liquidity_amount=liquidity_amount,
    )
)
print(f"remove: {remove_tx}")

primedelta.logout()
