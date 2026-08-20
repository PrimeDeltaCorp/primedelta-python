# Prime Delta Python Library

Official Python SDK for the [Prime Delta](https://primedelta.io) mint platform and on-chain DEX. Wraps account/KYC/order endpoints and signs transactions for the on-chain stock factory, AMM, and price-feed pools.

## Install

```bash
pip install primedelta
```

Requires Python >= 3.10.

## Quick start

```python
from decimal import Decimal
from primedelta import PrimeDelta, SwapSide

primedelta = PrimeDelta(
    private_key=...,
    web3_provider_url=...,
)
primedelta.login()

# Spend 10 dUSD buying AAPL on the DEX.
tx = primedelta.swap_exact_input(
    "AAPL",
    SwapSide.STABLECOIN_TO_STOCK,
    amount_in=Decimal("10"),
    min_amount_out=Decimal("0"),
)
```

## Mint platform examples

- [Login and logout](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/mint-platform/login_and_logout.py)
- [Stocks](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/mint-platform/stocks.py)
- [Deposit, withdraw, distributions](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/mint-platform/deposit_withdraw_distribution.py)
- [Buying and selling stocks (orders)](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/mint-platform/buying_and_selling_stocks.py)
- [Portfolio](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/mint-platform/portfolio.py)
- [Allowances, native DEL, DID reads](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/mint-platform/allowances_and_did.py) — `approve` / `allowance` / `revoke_approval`, `send_del`, `did_token_id` / `is_pro` / `is_valid`
- [Real-time price stream (logged in)](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/mint-platform/price_stream/prices_stream_logged.py)
- [Real-time price stream (public Pyth — parked since the 2026-07-31 free-Hermes shutdown; set PYTH_HERMES_BASE_URL to an authenticated endpoint to re-enable)](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/mint-platform/price_stream/prices_stream_not_logged.py)

## DEX examples

### Swaps

The router accepts dUSD on one side (`buyExact*`/`sellExact*`) or two non-dUSD tokens routed through dUSD (`swapExact*`, 2-hop). The SDK picks the right entrypoint per call.

- [dUSD ↔ stock (AAPL)](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/dex/swap.py) — `swap_exact_input` / `swap_exact_output` with `SwapSide`
- [dUSD ↔ AMM token (AMMT1, AMMT2)](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/dex/swap_amm.py) — same API, AMM symbol
- [Cross-dex token ↔ token](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/dex/swap_cross_dex.py) — `swap_token_to_token_exact_input` / `swap_token_to_token_exact_output` for AMM↔AMM, AMM↔stock, stock↔stock
- [Native DEL swaps](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/dex/swap_native.py) — `wrap_del` / `unwrap_del` plus regular swap on WDEL

> The stablecoin is **dUSD** on chain. `SwapSide.STABLECOIN_TO_STOCK` and `SwapSide.STOCK_TO_STABLECOIN` are the two single-hop directions; cross-dex swaps use the dedicated `swap_token_to_token_*` methods instead of `SwapSide`.

### Quoting

- [Pre-trade quoting & spot price](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/dex/quoting.py) — read-only `quote_swap` (V3 Quoter) and `spot_price` (slot0), no login required

### Liquidity

- [Price-feed pool liquidity](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/dex/liquidity_pricefeed.py) — `add_liquidity` / `remove_liquidity` with `PriceFeedAddLiquidity` / `PriceFeedRemoveLiquidity`
- [AMM (Uniswap V3) liquidity](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/dex/liquidity_amm.py) — concentrated-range positions via `AMMAddLiquidity` / `AMMRemoveLiquidity`
- [Full V3 position lifecycle](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/dex/v3_lifecycle.py) — `add` → `increase_liquidity` → `preview_fees` → `burn_position`

## Signers / wallets

A `Signer` is the whole wallet dependency (`address`, `sign_message`, submit-a-tx). `private_key=` is a thin wrapper for the raw-key case; everything else is identical regardless of signer.

- [Provisioning recipes](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/signers.py) — raw key, encrypted keystore, mnemonic, AWS KMS (`pip install "primedelta[kms]"`), and network switching
- [Browser wallet (MetaMask / EIP-6963)](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/examples/browser_wallet.py) — a local loopback bridge; the user signs in their own extension

## Networks

Addresses and ABIs ship inside the package under [`networks/`](https://github.com/PrimeDeltaCorp/primedelta-python/tree/main/src/primedelta/networks/). Pass `network="dev"` (default) or `network="testnet"` to `PrimeDelta(...)` — the backend base URL and SIWE signing domain follow the network automatically (no extra env). `PRIMEDELTA_BASE_URL` / `PRIMEDELTA_APP_URL` env vars still override for local stacks. To pin a different deployment, edit the network's JSON file — see [refreshing the network config after a redeploy](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/docs/refresh-network-config.md).

## Development

See [CONTRIBUTING.md](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/CONTRIBUTING.md) for local setup, tests, and formatting. Release notes live in [CHANGELOG.md](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/CHANGELOG.md).

## License

See [LICENSE](https://github.com/PrimeDeltaCorp/primedelta-python/blob/main/LICENSE).
