import os
from decimal import Decimal

from dotenv import find_dotenv, load_dotenv

from primedelta import PrimeDelta, SwapSide
from primedelta.settings import DEFAULT_NETWORK

load_dotenv(find_dotenv(".env.local") or find_dotenv(".env"))

primedelta = PrimeDelta(
    private_key=os.environ["PRIMEDELTA_TEST_PRIVATE_KEY"],
    web3_provider_url=os.environ["PRIMEDELTA_PROVIDER_URL"],
    network=os.environ.get("PRIMEDELTA_NETWORK", DEFAULT_NETWORK),
)
primedelta.login()

# AMM-token swaps go through the same router entrypoints as stocks; only the
# pool implementation differs (UniswapV3 vs. custom pricefeed). The SDK picks
# automatically based on which pool is registered for the symbol.

# Buy WDEL with 10 dUSD.
buy_tx = primedelta.swap_exact_input(
    "WDEL",
    SwapSide.STABLECOIN_TO_STOCK,
    amount_in=Decimal("10"),
    min_amount_out=Decimal("0"),
)
print(f"buy WDEL with dUSD: {buy_tx}")

wdel_received = primedelta.get_onchain_stock_balance("WDEL")
print(f"WDEL on chain: {wdel_received}")

# Sell half of the WDEL we just bought back to dUSD.
sell_tx = primedelta.swap_exact_input(
    "WDEL",
    SwapSide.STOCK_TO_STABLECOIN,
    amount_in=wdel_received / 2,
    min_amount_out=Decimal("0"),
)
print(f"sell WDEL for dUSD: {sell_tx}")

# Exact-output: buy exactly 0.05 WDEL, capping spend at 25 dUSD.
exact_out_tx = primedelta.swap_exact_output(
    "WDEL",
    SwapSide.STABLECOIN_TO_STOCK,
    amount_out=Decimal("0.05"),
    max_amount_in=Decimal("25"),
)
print(f"exact-out buy WDEL with dUSD: {exact_out_tx}")

# Exact-output sell: receive exactly 1 dUSD by selling WDEL, capping the WDEL
# spent at half of what we still hold.
remaining_wdel = primedelta.get_onchain_stock_balance("WDEL")
exact_out_sell_tx = primedelta.swap_exact_output(
    "WDEL",
    SwapSide.STOCK_TO_STABLECOIN,
    amount_out=Decimal("1"),
    max_amount_in=remaining_wdel / 2,
)
print(f"exact-out sell WDEL for dUSD: {exact_out_sell_tx}")

primedelta.logout()
