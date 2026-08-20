"""Read-only pre-trade quoting — no login required.

`quote_swap` runs the Uniswap-V3 Quoter to preview the output (or input) of an
AMM swap; `spot_price` reads the pool's current dUSD-per-token from slot0.
"""

import os
from decimal import Decimal

from dotenv import find_dotenv, load_dotenv

from primedelta import PrimeDelta, SwapSide

load_dotenv(find_dotenv(".env.local") or find_dotenv(".env"))

primedelta = PrimeDelta(
    private_key=os.environ["PRIMEDELTA_TEST_PRIVATE_KEY"],
    web3_provider_url=os.environ["PRIMEDELTA_PROVIDER_URL"],
)

# Spot price of AMMT1 in dUSD (from the pool's slot0).
print("AMMT1 spot price (dUSD):", primedelta.spot_price("AMMT1"))

# How much AMMT1 would 10 dUSD buy (exact-input)?
out = primedelta.quote_swap(
    "AMMT1", SwapSide.STABLECOIN_TO_STOCK, Decimal("10"), exact="input"
)
print("10 dUSD -> AMMT1:", out)

# How much dUSD to receive exactly 1 AMMT1 (exact-output)?
needed = primedelta.quote_swap(
    "AMMT1", SwapSide.STABLECOIN_TO_STOCK, Decimal("1"), exact="output"
)
print("dUSD needed for 1 AMMT1:", needed)
