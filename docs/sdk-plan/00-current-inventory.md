# PrimeDelta Python SDK — Current Inventory (as-is)

> Snapshot of what `primedelta` (v0.1.0) ships **today**, from a full read of
> `src/primedelta/` at `/home/ubuntu/primedelta/primedelta-python`. Written 2026-08-18.
>
> ⚠️ CONTRADICTION TO RESOLVE: product owner says the repo is "outdated, unmaintained
> since ~March 2026, a prior maintainer broke it, nothing worked." But the code read
> here references July‑2026 events (Pyth Hermes park 2026‑07‑31), cross‑dex swaps,
> AMMT, WDEL, folding, and rich Besu error/nonce handling — i.e. **recent, polished
> work**. Git-state recon must reconcile: is origin/main the broken March state with
> good UNPUSHED work locally (H1), or is this local state current/pushed (H2)?

## 1. Package shape

```
src/primedelta/
├── __init__.py            # public exports (PrimeDelta, SwapSide, params, exceptions, types.*)
├── primedelta.py          # PrimeDelta — the main façade (auth, funds, orders, DEX, balances)
├── primedelta_client.py   # PrimeDeltaClient — thin REST wrapper over the backend API
├── contracts.py           # dataclasses: ContractRef / CoreContracts / StockPools / Contracts
├── settings.py            # env-driven config (base URLs, SIWE text, Pyth base URL)
├── types.py               # domain dataclasses + enums (Portfolio, Order, Stock, ...)
├── dex/
│   ├── params.py          # SwapSide, PoolType, Add/RemoveLiquidity param dataclasses
│   └── handlers.py        # _RouterSwapHandler / _DclexPoolHandler / _AMMPoolHandler
└── networks/
    ├── __init__.py        # load(network) → Contracts (addresses from <net>.json, ABIs from abis/)
    ├── dev.json           # chain 2028 addresses (only network shipped)
    └── abis/*.json        # 12 ABIs: stablecoin, vault, factory, digital_identity,
                           # dex_router, position_manager, oracle, wdel,
                           # dclex_pool, univ3_pool, univ3_factory, erc20
```

- **Runtime deps:** `web3==7.15.0`, `siwe==4.4.0`, `requests==2.33.0`, `sseclient==0.0.27`.
- **Dev deps (pyproject group):** `pytest`, `python-dotenv`. **requirements-dev.txt** also pins
  `pytest==9.0.3`, `types-requests`, `black==26.3.1`, `isort`, `mypy==1.10.0` (two dev-dep
  sources — not aligned).
- **Python:** `>=3.10`. **Build backend:** setuptools. Ships `networks/*.json` + `abis/*.json`.

## 2. Public API surface (the `PrimeDelta` façade)

### Auth / identity
- `login()` — SIWE (nonce → `SiweMessage` → local-key sign → POST `/users/verify/`, store token).
- `logout()`, `logged_in() -> bool`, `get_account_status() -> AccountStatus`.
- `verification_url()`, `open_verification_page()` — KYC is in-browser, not via SDK.
- `claim_digital_identity() -> tx_hash` — backend signs → on-chain `DigitalIdentity.mint(...)`.

### Funds — stablecoin (dUSD, 6 decimals)
- `deposit_stablecoin(amount)` — ERC20 `transfer` → vault.
- `request_stablecoin_withdrawal(amount)` → id; `claim_stablecoin_withdrawal(id)` → `Vault.withdraw`.
- `get_stablecoin_available_balance()`, `get_stablecoin_total_balance()`, `get_onchain_stablecoin_balance()`.

### Funds — stock tokens (18 decimals)
- `deposit_stock_token(symbol, amount)` → `Factory.burnStocks`.
- `request_stock_withdrawal(symbol, amount)` → id; `claim_stock_withdrawal(id)` → `Factory.mintStocks`.
- `claimable_withdrawals()`, `get_stock_available_balance(symbol)`, `get_stock_total_balance(symbol)`,
  `get_onchain_stock_balance(symbol)`.

### Native DEL / WDEL
- `get_native_del_balance()`, `wrap_del(amount)` (`WDEL.deposit`), `unwrap_del(amount)` (`WDEL.withdraw`).

### Broker orders (mint platform)
- `send_limit_order(side, symbol, amount, price_limit, date_of_cancellation?)` → `/orders/limit/{buy|sell}/`.
- `send_sell_market_order(symbol, amount)` → `/orders/market/sell/`.  **(no market BUY)**
- `cancel_order(id)`, `get_order_status(id)`, `open_orders()`, `closed_orders()`.

### Portfolio / history / market
- `portfolio()`, `pending_transfers()`, `closed_transfers()`, `distributions()`,
  `stocks() -> dict[str, Stock]`, `is_market_open()`.

### Prices (streaming, SSE)
- `prices_stream(symbols?)` — logged in → broker `/prices-stream/`; else Pyth (parked).
- `pyth_prices_stream(symbols?)` — needs `PYTH_HERMES_BASE_URL` (off by default since 2026‑07‑31).

### DEX — swaps (on-chain via `DclexRouter`, signed FIOracle prices)
- `swap_exact_input` / `swap_exact_output` (`SwapSide` STABLECOIN_TO_STOCK | STOCK_TO_STABLECOIN)
  → `buy/sellExact{Input,Output}`.
- `swap_token_to_token_exact_input` / `_exact_output` — 2‑hop cross-dex through dUSD
  (`swapExact{Input,Output}`).

### DEX — liquidity
- `add_liquidity` / `remove_liquidity` (PriceFeed = DclexPool `add/removeLiquidity`;
  AMM = V3 `NPM.mint` / `NPM.multicall(decreaseLiquidity, collect)`).
- `collect_fees(position_id)`, `lp_positions() -> list[int]`, `lp_position(id) -> LPPosition`.

## 3. Backend client (`PrimeDeltaClient`)
Thin `requests` wrapper. Token auth (`Authorization: Token <t>`; `Bearer` on `/signed-prices/`).
Error mapping: 400→`APIError(errorCode)`, 401→`NotLoggedIn`, 403→`AuthorizationError`.
SSE via `sseclient` for broker + Pyth streams.

## 4. Networks / contracts registry
- Addresses ship **inside** the package (`networks/<net>.json`); ABIs in `networks/abis/`.
  Rationale (in `networks/__init__.py`): backend `/contracts/` drifts behind redeploys.
- **Only `dev` (chain 2028) ships.** No testnet (7357) / mainnet (4109). No runtime chain switch.
- Pools resolved on-chain: `Router.allStockTokens()` + `symbol()`; AMM via
  `NPM.factory().getPool(stock, dUSD, 3000)`.

## 5. Signing / wallet model (TODAY)
- `PrimeDelta(private_key: str, web3_provider_url: str, network="dev")` — **raw hex key only**.
  `eth_account` `LocalAccount` signs SIWE + every tx locally.
- Besu/PoA hardening: `ExtraDataToPOAMiddleware`; local nonce tracker with "nonce too low"
  parse‑and‑retry; fixed 5M gas (skips `estimate_gas` to dodge a Besu nonce race);
  receipt‑wait between chained txs; rich revert decode (`Error(string)`/`Panic`,
  `debug_traceCall` deepest‑error walk) → `TransactionFailed`.

## 6. Error taxonomy
`NotEnoughFunds`, `AccountNotVerified`, `DigitalIdentityAlreadyClaimed`, `WithdrawalNotFound`,
`WdelNotConfigured`, `TransactionFailed` (rich), `PoolNotFound`, `RouterNotConfigured`,
`PositionManagerNotConfigured`, `NotLoggedIn`, `AuthorizationError`, `APIError`,
`UserSignedMessageVerificationError`.

## 7. Examples shipped
- `examples/mint-platform/`: login_and_logout, stocks, deposit_withdraw_distribution,
  buying_and_selling_stocks, portfolio, price_stream/{logged, not_logged}.
- `examples/dex/`: swap, swap_amm, swap_cross_dex, swap_native, liquidity_pricefeed, liquidity_amm.

## 8. First-glance gaps (confirm via recon)
- **Wallet connection**: raw private key only — no keystore/mnemonic/browser/external signer,
  no `Signer` abstraction. (Primary theme of this plan.)
- **Only `dev` network**; no testnet/mainnet configs, no chain-switch story.
- **No quoting/preview** of swap output or LP amounts before sending (no Quoter helper).
- **No market BUY** order (only market SELL); limit buy/sell exist.
- **Type hints not distributed** (no `py.typed`).
- **No CI / release automation** apparent; **not on PyPI** (confirm).
- **Newer backend surfaces** (admin→user messages, dividend eligibility/info, fiat deposit)
  likely unwrapped (confirm vs backend).
- **Two unaligned dev-dependency sources** (pyproject `dependency-groups.dev` vs `requirements-dev.txt`).
