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

# Cross-dex swaps trade two non-dUSD tokens; the router routes input→dUSD→output
# internally. Either leg may be a custom (pricefeed) or AMM pool — the router
# picks per-token.

# Seed WDEL (wrap native DEL) and AAPL (with dUSD) so the cross-dex swaps have
# something to spend.
primedelta.wrap_del(Decimal("2"))
primedelta.swap_exact_input(
    "AAPL",
    SwapSide.STABLECOIN_TO_STOCK,
    amount_in=Decimal("20"),
    min_amount_out=Decimal("0"),
)
wdel_balance = primedelta.get_onchain_stock_balance("WDEL")
aapl_balance = primedelta.get_onchain_stock_balance("AAPL")
print(f"seeded WDEL={wdel_balance}, AAPL={aapl_balance}")

# Stock → AMM: trade AAPL for WDEL.
stock_to_amm_tx = primedelta.swap_token_to_token_exact_input(
    input_symbol="AAPL",
    output_symbol="WDEL",
    amount_in=aapl_balance / 4,
    min_amount_out=Decimal("0"),
)
print(f"AAPL → WDEL: {stock_to_amm_tx}")

# AMM → Stock: trade some WDEL for AAPL.
amm_to_stock_tx = primedelta.swap_token_to_token_exact_input(
    input_symbol="WDEL",
    output_symbol="AAPL",
    amount_in=primedelta.get_onchain_stock_balance("WDEL") / 4,
    min_amount_out=Decimal("0"),
)
print(f"WDEL → AAPL: {amm_to_stock_tx}")

# Exact-output cross-dex: buy exactly 0.01 AAPL by spending at most 5 WDEL.
exact_out_tx = primedelta.swap_token_to_token_exact_output(
    input_symbol="WDEL",
    output_symbol="AAPL",
    amount_out=Decimal("0.01"),
    max_amount_in=Decimal("5"),
)
print(f"exact-out WDEL → AAPL: {exact_out_tx}")

# Exact-output Stock → AMM: buy exactly 0.02 WDEL by spending at most 0.5 AAPL.
exact_out_stock_to_amm_tx = primedelta.swap_token_to_token_exact_output(
    input_symbol="AAPL",
    output_symbol="WDEL",
    amount_out=Decimal("0.02"),
    max_amount_in=Decimal("0.5"),
)
print(f"exact-out AAPL → WDEL: {exact_out_stock_to_amm_tx}")

primedelta.logout()
