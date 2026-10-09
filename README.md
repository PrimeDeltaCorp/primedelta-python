# Prime Delta Python Library

Official Python SDK for the [Prime Delta](https://primedelta.io) mint platform and on-chain DEX. Wraps account/KYC/order endpoints and signs transactions for the on-chain stock factory, oracle-free, and oracle-priced pools.

## Install

The SDK is public but **not yet on PyPI** (a plain `pip install primedelta` is a
fast-follow). Install from git:

```bash
pip install "primedelta @ git+https://github.com/PrimeDeltaCorp/primedelta-python.git"
# AWS KMS custody adds the [kms] extra:
#   pip install "primedelta[kms] @ git+https://github.com/PrimeDeltaCorp/primedelta-python.git"
# from a local checkout:
#   pip install -e .
```

Requires Python >= 3.10.

## Quick start

```python
from decimal import Decimal
from primedelta import PrimeDelta, SwapSide

primedelta = PrimeDelta(
    private_key=...,
    web3_provider_url="https://chain.testnet.primedelta.io",
    network="testnet",
)

primedelta.is_market_open()
primedelta.stocks()
primedelta.instrument_kind("WDEL")
primedelta.spot_price("WDEL")

primedelta.login()
primedelta.wrap_del(Decimal("1"))

# Sell 1 WDEL for dUSD on the 24/7 oracle-free pool. Quote first, then derive a real
# min_amount_out from a slippage budget — never pass Decimal("0") in production.
quote = primedelta.quote_swap("WDEL", SwapSide.STOCK_TO_STABLECOIN, Decimal("1"))
tx = primedelta.swap_exact_input(
    "WDEL",
    SwapSide.STOCK_TO_STABLECOIN,
    amount_in=Decimal("1"),
    min_amount_out=primedelta.min_out_from_quote(quote, slippage_bps=100),  # 1%
)
```

The reads before `login()` need no login. WDEL is native DEL wrapped 1:1 and the
only oracle-free token on testnet (`instrument_kind("WDEL") == "amm"`), so it
trades 24/7. The first write is `wrap_del`;
`primedelta.craft(lambda: primedelta.wrap_del(Decimal("1")))` returns the same
transaction unsigned instead of sending it. Swaps need liquidity in the testnet
WDEL/dUSD pool: while the pool is empty, `quote_swap` and `swap_exact_input`
revert.

> Oracle-priced tokens (e.g. `AAPL`) trade only in US market hours; the SDK
> classifies instruments via `instrument_kind(symbol)` and raises `MarketClosed`
> when the signed price is unavailable.

## For autonomous agents

If an AI agent operates this SDK to trade, read [**AGENTS.md**](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/AGENTS.md) — the operating handbook: capability map, the mandatory safe pattern (quote → min-out → market-hours check → send), and the fair-use / anti-abuse policy.

## Mint platform examples

- [Login and logout](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/mint-platform/login_and_logout.py)
- [Stocks](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/mint-platform/stocks.py)
- [Deposit, withdraw, distributions](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/mint-platform/deposit_withdraw_distribution.py)
- [Buying and selling stocks (orders)](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/mint-platform/buying_and_selling_stocks.py)
- [Portfolio](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/mint-platform/portfolio.py)
- [AI agents](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/mint-platform/ai_agents.py) — `confirm_ai_agent` (signed approval), `request_ai_agent_approval` + `link_ai_agent`, `get_my_ai_agents`, `fund_ai_agent` / `return_to_main`, `get_ai_agent_portfolio` / `get_ai_agent_open_orders`, `set_ai_agent_policy`, `close_ai_agent` / `reopen_ai_agent`
- [Allowances, native DEL, DID reads](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/mint-platform/allowances_and_did.py) — `approve` / `allowance` / `revoke_approval`, `send_del`, `did_token_id` / `is_pro` / `is_valid`
- [Real-time price stream (logged in)](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/mint-platform/price_stream/prices_stream_logged.py)
- [Real-time price stream (public Pyth — parked since the 2026-07-31 free-Hermes shutdown; set PYTH_HERMES_BASE_URL to an authenticated endpoint to re-enable)](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/mint-platform/price_stream/prices_stream_not_logged.py)

## DEX examples

### Swaps

The router accepts dUSD on one side (`buyExact*`/`sellExact*`) or two non-dUSD tokens routed through dUSD (`swapExact*`, 2-hop). The SDK picks the right entrypoint per call.

- [dUSD ↔ stock (AAPL)](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/dex/swap.py) — `swap_exact_input` / `swap_exact_output` with `SwapSide`
- [dUSD ↔ oracle-free token (WDEL)](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/dex/swap_amm.py) — same API, oracle-free symbol
- [Cross-dex token ↔ token](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/dex/swap_cross_dex.py) — `swap_token_to_token_exact_input` / `swap_token_to_token_exact_output` for oracle-free↔stock (WDEL↔AAPL) and stock↔stock
- [Native DEL swaps](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/dex/swap_native.py) — `wrap_del` / `unwrap_del` plus regular swap on WDEL

> The stablecoin is **dUSD** on chain. `SwapSide.STABLECOIN_TO_STOCK` and `SwapSide.STOCK_TO_STABLECOIN` are the two single-hop directions; cross-dex swaps use the dedicated `swap_token_to_token_*` methods instead of `SwapSide`.

### Quoting

- [Pre-trade quoting & spot price](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/dex/quoting.py) — read-only `quote_swap` (V3 Quoter) and `spot_price` (slot0), no login required

### Liquidity

- [Oracle-priced pool liquidity](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/dex/liquidity_pricefeed.py) — `add_liquidity` / `remove_liquidity` with `PriceFeedAddLiquidity` / `PriceFeedRemoveLiquidity`
- [Oracle-free (Uniswap V3) liquidity](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/dex/liquidity_amm.py) — concentrated-range positions via `AMMAddLiquidity` / `AMMRemoveLiquidity`
- [Full V3 position lifecycle](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/dex/v3_lifecycle.py) — `add` → `increase_liquidity` → `preview_fees` → `burn_position`

## Signers / wallets

A `Signer` is the whole wallet dependency (`address`, `sign_message`, submit-a-tx). `private_key=` is a thin wrapper for the raw-key case; everything else is identical regardless of signer.

- [Provisioning recipes](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/signers.py) — raw key, encrypted keystore, mnemonic, AWS KMS (`pip install "primedelta[kms]"`), and network switching
- [Browser wallet (MetaMask / EIP-6963)](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/browser_wallet.py) — a local loopback bridge; the user signs in their own extension

## Networks

Addresses and ABIs ship inside the package under [`networks/`](https://github.com/PrimeDeltaCorp/primedelta-python/tree/main/src/primedelta/networks/). `network` defaults to `"testnet"`, the public network — the backend base URL and SIWE signing domain follow the network automatically (no extra env). `PRIMEDELTA_BASE_URL` / `PRIMEDELTA_APP_URL` env vars still override for local stacks. Before the first login, send or `craft`, the client checks that `web3_provider_url` is on the network's chain and raises `NetworkMismatch` if it is not. To pin a different deployment, edit the network's JSON file.

## Development

See [CONTRIBUTING.md](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/CONTRIBUTING.md) for local setup, tests, and formatting, and [RELEASING.md](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/RELEASING.md) for cutting a release. Release notes live in [CHANGELOG.md](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/CHANGELOG.md).

## License

See [LICENSE](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/LICENSE).
