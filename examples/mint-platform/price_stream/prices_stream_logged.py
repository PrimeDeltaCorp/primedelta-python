import os

from dotenv import find_dotenv, load_dotenv

from primedelta import PrimeDelta
from primedelta.settings import DEFAULT_NETWORK

load_dotenv(find_dotenv(".env.local") or find_dotenv(".env"))

primedelta = PrimeDelta(
    private_key=os.environ["PRIMEDELTA_TEST_PRIVATE_KEY"],
    web3_provider_url=os.environ["PRIMEDELTA_PROVIDER_URL"],
    network=os.environ.get("PRIMEDELTA_NETWORK", DEFAULT_NETWORK),
)

# Authenticated stream — broker feed for verified accounts. `symbols` is
# ignored on this path (the broker pushes everything the user is entitled to).
primedelta.login()
for price in primedelta.prices_stream():
    print(f"{price.symbol}: {price.last_price} @ {price.timestamp}")
primedelta.logout()
