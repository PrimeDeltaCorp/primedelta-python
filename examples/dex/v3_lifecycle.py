"""Full AMM (Uniswap-V3) position lifecycle: add -> increase -> preview -> burn.

The range is read from the WDEL/dUSD pool at runtime and sits entirely on the
dUSD side of the current tick, so the position holds only dUSD — handy when the
wallet holds no WDEL. WDEL is token0 and dUSD is token1 in the testnet pool, so
that side is below the current tick. Token ordering is per-pool (by address);
widen or centre the range for a two-sided position.
"""

import os
from decimal import Decimal

from dotenv import find_dotenv, load_dotenv

from primedelta import AMMAddLiquidity, PrimeDelta
from primedelta.settings import DEFAULT_NETWORK

load_dotenv(find_dotenv(".env.local") or find_dotenv(".env"))

primedelta = PrimeDelta(
    private_key=os.environ["PRIMEDELTA_TEST_PRIVATE_KEY"],
    web3_provider_url=os.environ["PRIMEDELTA_PROVIDER_URL"],
    network=os.environ.get("PRIMEDELTA_NETWORK", DEFAULT_NETWORK),
)
primedelta.login()

w3 = primedelta._web3
contracts = primedelta._get_contracts()
dusd = w3.to_checksum_address(contracts.core.stablecoin.address)
npm = w3.eth.contract(
    address=w3.to_checksum_address(contracts.core.position_manager.address),
    abi=contracts.core.position_manager.abi,
)
factory = w3.eth.contract(
    address=npm.functions.factory().call(), abi=contracts.pool_abis["univ3_factory"]
)
pool = w3.eth.contract(
    address=factory.functions.getPool(
        w3.to_checksum_address(contracts.core.wdel.address), dusd, 3000
    ).call(),
    abi=contracts.pool_abis["univ3_pool"],
)
tick = pool.functions.slot0().call()[1]
spacing = pool.functions.tickSpacing().call()
if pool.functions.token1().call() == dusd:
    tick_upper = (tick // spacing - 1) * spacing
    tick_lower = tick_upper - 100 * spacing
else:
    tick_lower = (tick // spacing + 2) * spacing
    tick_upper = tick_lower + 100 * spacing
print("current tick:", tick, "range:", tick_lower, tick_upper)

# Open a single-sided dUSD position on the WDEL pool.
add_tx = primedelta.add_liquidity(
    AMMAddLiquidity(
        symbol="WDEL",
        tick_lower=tick_lower,
        tick_upper=tick_upper,
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
