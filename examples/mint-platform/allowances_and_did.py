"""ERC20 allowances, a native DEL transfer, and on-chain DID reads."""

import os
from decimal import Decimal

from dotenv import find_dotenv, load_dotenv

from primedelta import PrimeDelta

load_dotenv(find_dotenv(".env.local") or find_dotenv(".env"))

primedelta = PrimeDelta(
    private_key=os.environ["PRIMEDELTA_TEST_PRIVATE_KEY"],
    web3_provider_url=os.environ["PRIMEDELTA_PROVIDER_URL"],
)
primedelta.login()

spender = primedelta._get_contracts().core.dex_router.address

# Allowances (amounts are human units; the SDK scales by token decimals).
print("current dUSD allowance to router:", primedelta.allowance("dUSD", spender))
approve_tx = primedelta.approve("dUSD", spender, Decimal("100"))
print("approve 100 dUSD:", approve_tx)
print("after approve:", primedelta.allowance("dUSD", spender))
revoke_tx = primedelta.revoke_approval("dUSD", spender)
print("revoke:", revoke_tx)

# Native DEL transfer.
send_tx = primedelta.send_del(primedelta._signer.address, Decimal("0.001"))
print("sent 0.001 DEL to self:", send_tx)

# On-chain Digital Identity reads.
print("DID token id:", primedelta.did_token_id())
print("is pro:", primedelta.is_pro())
print("is valid:", primedelta.is_valid())

primedelta.logout()
