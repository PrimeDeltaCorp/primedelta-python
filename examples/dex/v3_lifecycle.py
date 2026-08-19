"""Full AMM (Uniswap-V3) position lifecycle: add -> increase -> preview -> burn.

The range below sits entirely under the current tick, so the position holds only
dUSD (token1) — handy when the wallet is short on the stock leg. Widen/centre the
range for a two-sided position.
"""

import os
from decimal import Decimal

from dotenv import find_dotenv, load_dotenv

from primedelta import AMMAddLiquidity, PrimeDelta

load_dotenv(find_dotenv(".env.local") or find_dotenv(".env"))

primedelta = PrimeDelta(
    private_key=os.environ["PRIMEDELTA_TEST_PRIVATE_KEY"],
    web3_provider_url=os.environ["PRIMEDELTA_PROVIDER_URL"],
)
primedelta.login()

# Open a single-sided dUSD position on the AMMT1 pool.
add_tx = primedelta.add_liquidity(
    AMMAddLiquidity(
        symbol="AMMT1",
        tick_lower=-260040,
        tick_upper=-253080,
        amount_stock_desired=Decimal("0"),
        amount_stablecoin_desired=Decimal("10"),
        amount_stock_min=Decimal("0"),
        amount_stablecoin_min=Decimal("0"),
    )
)
print("add_liquidity:", add_tx)

position_id = primedelta.lp_positions()[-1]
print(
    "new position:",
    position_id,
    "liquidity:",
    primedelta.lp_position(position_id).liquidity,
)

# Add more to the same position.
inc_tx = primedelta.increase_liquidity(
    position_id, amount_stock=Decimal("0"), amount_stablecoin=Decimal("5")
)
print("increase_liquidity:", inc_tx)

# Preview collectable fees (static simulation — no state change).
print("collectable (stock, stablecoin):", primedelta.preview_fees(position_id))

# Close it out: decrease-all + collect + burn in one multicall.
burn_tx = primedelta.burn_position(position_id)
print("burn_position:", burn_tx)

primedelta.logout()
