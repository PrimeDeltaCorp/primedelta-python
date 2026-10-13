import os
from decimal import Decimal

from dotenv import find_dotenv, load_dotenv

from primedelta import (
    AMMAddLiquidity,
    AMMRemoveLiquidity,
    PrimeDelta,
)
from primedelta.settings import DEFAULT_NETWORK

load_dotenv(find_dotenv(".env.local") or find_dotenv(".env"))

primedelta = PrimeDelta(
    private_key=os.environ["PRIMEDELTA_TEST_PRIVATE_KEY"],
    web3_provider_url=os.environ["PRIMEDELTA_PROVIDER_URL"],
    network=os.environ.get("PRIMEDELTA_NETWORK", DEFAULT_NETWORK),
)
primedelta.login()

# WDEL is native DEL wrapped 1:1; wrap some before LPing.
wrap_tx = primedelta.wrap_del(Decimal("2"))
print(f"wrap:   {wrap_tx}")

wdel_balance = primedelta.get_onchain_stock_balance("WDEL")
print(f"WDEL on chain: {wdel_balance}")

# Add full-range liquidity (-887220/887220 is the full range for fee tier 3000).
# Pool draws the matching ratio at current price; caps prevent over-spend.
add_tx = primedelta.add_liquidity(
    AMMAddLiquidity(
        symbol="WDEL",
        tick_lower=-887220,
        tick_upper=887220,
        amount_stock_desired=wdel_balance / 2,
        amount_stablecoin_desired=Decimal("10"),
        amount_stock_min=Decimal("0"),
        amount_stablecoin_min=Decimal("0"),
    )
)
print(f"add:    {add_tx}")

# Discover the position NFT we just minted (it's the most recent one we own).
positions = primedelta.lp_positions()
position_id = positions[-1]
info = primedelta.lp_position(position_id)
print(f"position_id={position_id} liquidity={info.liquidity}")

# Burn all liquidity from this position.
remove_tx = primedelta.remove_liquidity(
    AMMRemoveLiquidity(
        position_id=position_id,
        liquidity=info.liquidity,
        amount_stock_min=Decimal("0"),
        amount_stablecoin_min=Decimal("0"),
    )
)
print(f"remove: {remove_tx}")

# Sweep any accumulated fees + the just-decreased liquidity.
collect_tx = primedelta.collect_fees(position_id)
print(f"collect:{collect_tx}")

primedelta.logout()
