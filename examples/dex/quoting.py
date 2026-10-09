"""Read-only pre-trade quoting — no login required.

`quote_swap` runs the Uniswap-V3 Quoter to preview the output (or input) of an
AMM swap; `spot_price` reads the pool's current dUSD-per-token from slot0. The
Quoter needs liquidity in the pool: while the testnet WDEL/dUSD pool is empty,
`quote_swap` reverts and only `spot_price` answers.
"""

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

# Spot price of WDEL in dUSD (from the pool's slot0).
print("WDEL spot price (dUSD):", primedelta.spot_price("WDEL"))

# How much WDEL would 10 dUSD buy (exact-input)?
out = primedelta.quote_swap(
    "WDEL", SwapSide.STABLECOIN_TO_STOCK, Decimal("10"), exact="input"
)
print("10 dUSD -> WDEL:", out)

# How much dUSD to receive exactly 1 WDEL (exact-output)?
needed = primedelta.quote_swap(
    "WDEL", SwapSide.STABLECOIN_TO_STOCK, Decimal("1"), exact="output"
)
print("dUSD needed for 1 WDEL:", needed)
