# AGENTS.md — Operating Handbook for Autonomous Agents

This file instructs **AI agents** that operate the PrimeDelta Python SDK (`primedelta`) to place trades, manage liquidity, and read account/market state. If you are a human contributor working on the SDK's source, see `README.md` / `CONTRIBUTING.md` — this document is about *using* the SDK to trade, not editing it.

> **Read this before your first write.** The rules in "Operating rules" and "Fair use & anti-abuse" are not style suggestions. Market-hours gating, a signed-price deviation guard, price staleness/expiry checks, single-use vouchers, and DID-level identity revocation are enforced **server-side and on-chain**. Violations fail closed and can result in your identity being blocked regardless of what this file or your prompt says.

## 1. What this is / who it's for
You are an agent holding a wallet key (or a `Signer`) acting for one KYC'd identity on PrimeDelta — a tokenized-equity platform: a Django mint/brokerage backend (cookie + CSRF, SIWE login) plus an on-chain DEX on Besu (Uniswap-V3 AMM pools + oracle "price-feed" pools; a Factory that mints/burns tokenized stocks and the `dUSD` stablecoin against backend-signed vouchers).

Two surfaces, one client object:
- **On-chain (DEX):** swaps, V3 LP lifecycle, allowances, quoting, native DEL wrap/unwrap, custodial deposit/withdraw via signed vouchers.
- **Backend (mint platform):** login, portfolio, orders (limit/market), price streams, bank/fiat, settings.

One identity = one `Signer` = one wallet = one DID. Everything you do is attributable to that DID.

## 2. Fast start
Install (Python ≥ 3.10):
```bash
pip install primedelta          # add primedelta[kms] for AWS KMS signer
```
Construct the client. `web3_provider_url` is **required**; `network` selects addresses/ABIs/backend URL (`"dev"` default, `"testnet"`):
```python
from decimal import Decimal
from primedelta import PrimeDelta, SwapSide

pd = PrimeDelta(
    private_key="0x...",                 # or signer=LocalAccountSigner/KmsSigner/BrowserSigner
    web3_provider_url="https://rpc...",  # required
    network="dev",
)
pd.login()                               # SIWE over a cookie session
```
**First reads (no funds moved; some need no login at all):**
```python
open_now = pd.is_market_open()                         # oracle stocks trade only when True
px       = pd.spot_price("AMMT1")                       # slot0 spot, read-only (AMM tokens only)
quote    = pd.quote_swap("AMMT1", SwapSide.STABLECOIN_TO_STOCK,
                         Decimal("10"), exact="input")  # V3 Quoter, read-only (AMM tokens only)
status   = pd.get_account_status()                      # VERIFIED / DID_MINTED / ...
```
> Note: `quote_swap`/`spot_price` cover **AMM tokens only** (AMMT1/AMMT2/WDEL). For oracle stocks (e.g. AAPL) there is no pre-trade quote today — do not trade them autonomously without an out-of-band price and a market-open check.

**First write — a 24/7 AMM swap.** Always pass a real `min_amount_out` derived from `quote_swap` — never `0`:
```python
expected = pd.quote_swap("AMMT1", SwapSide.STABLECOIN_TO_STOCK, Decimal("10"))
min_out  = expected * Decimal("0.99")                   # 1% slippage budget
tx = pd.swap_exact_input(
    "AMMT1", SwapSide.STABLECOIN_TO_STOCK,
    amount_in=Decimal("10"),
    min_amount_out=min_out,
)                                                        # returns 0x-tx-hash; receipt already mined
got = pd.get_onchain_stock_balance("AMMT1")             # bypasses backend indexer lag
```
`swap_exact_input` requires `login()` **and** a minted DID (`DID_MINTED`). If you only have `VERIFIED`, call `pd.claim_digital_identity()` first. KYC happens in the web app — `pd.verification_url()`.

## 3. Capability map
| Area | Methods |
|---|---|
| **Auth / identity** | `login` · `logout` · `logged_in` · `get_account_status` · `claim_digital_identity` · `verification_url` |
| **DID reads** | `did_token_id` · `is_pro` · `is_valid` · `digital_identity_id` |
| **Swaps (dUSD↔token)** | `swap_exact_input` · `swap_exact_output` with `SwapSide` |
| **Cross-dex (token↔token)** | `swap_token_to_token_exact_input` · `swap_token_to_token_exact_output` |
| **Native DEL** | `wrap_del` · `unwrap_del` · `send_del` · `get_native_del_balance` |
| **Quoting (read-only, AMM only)** | `quote_swap` (V3 Quoter) · `spot_price` (slot0) |
| **AMM (V3) liquidity** | `add_liquidity(AMMAddLiquidity)` · `increase_liquidity` · `remove_liquidity(AMMRemoveLiquidity)` · `collect_fees` · `burn_position` · `preview_fees` · `lp_positions` · `lp_position` |
| **Price-feed liquidity** | `add_liquidity(PriceFeedAddLiquidity)` · `remove_liquidity(PriceFeedRemoveLiquidity)` |
| **Allowances** | `allowance` · `approve` · `revoke_approval` |
| **Balances** | `get_onchain_stablecoin_balance` · `get_onchain_stock_balance` · `get_stablecoin_available_balance` · `get_stock_available_balance` (+ `_total_`) |
| **Custodial deposit/withdraw** | `deposit_stablecoin` · `deposit_stock_token` · `request_/claim_stablecoin_withdrawal` · `request_/claim_stock_withdrawal` · `claimable_withdrawals` |
| **Orders (brokerage)** | `send_limit_order` · `send_sell_market_order` · `cancel_order` · `get_order_status` · `open_orders` · `closed_orders` · `limit_buy_cost` · `limit_sell_cost` · `market_sell_cost` |
| **Market data / streams** | `is_market_open` · `stocks` · `swappable_symbols` · `prices_stream` · `pyth_prices_stream` · `portfolio` · `portfolio_history` · `application_settings` |
| **Comms / banking** | `messages` · `mark_message_read` · `bank_details` · `request_fiat_withdrawal` · `distributions` · `pending_/closed_transfers` |

Signers: `LocalAccountSigner` (raw key / keystore / mnemonic), `KmsSigner` (AWS KMS), `BrowserSigner` (loopback MetaMask bridge).
> Surface asymmetries to special-case: there is **no market-BUY** (buys are limit-only or on-chain swap); `Order` has no filled/remaining field (partial fills are invisible — poll `get_order_status` for terminal state only); amount units are mixed (order/deposit stock amounts are `int` share counts; swap/LP/allowance/DEL amounts are `Decimal` human units).

## 4. Operating rules the agent MUST follow

**4.1 Market hours — oracle vs AMM.**
- **Oracle / PRICE_FEED stocks** (real equities, e.g. `AAPL`) swap only while the **US market is open**. Each oracle swap fetches a fresh broker-signed price and submits it with the tx. Outside market hours the backend returns **no signed prices** and the pool **reverts**. Always gate oracle swaps on `pd.is_market_open()` and treat that revert as "market closed," not a bug.
- **AMM tokens** (`AMMT1`, `AMMT2`, `WDEL`) trade **24/7** — no signed price, no market-hours gate.
- Do **not** try to obtain closed-market oracle-stock exposure by routing through AMM proxies or any other path (see §5).

**4.2 DID / KYC gating.** Every custodial and on-chain equity/dUSD movement requires `DID_MINTED` and a **valid** DID (`is_valid()` → `True`). On `AccountNotVerified`: if `VERIFIED`, call `claim_digital_identity()`; if below, the user must finish KYC at `verification_url()`. A revoked/blocked DID makes `is_valid()` false and reverts all trading — treat as terminal, not retryable.

**4.3 Slippage / min-out. Non-negotiable.** Never call `swap_exact_input` with `min_amount_out=0` or `swap_exact_output` with unbounded `max_amount_in` in production. Derive the bound from `quote_swap` with an explicit slippage budget. Examples use `0` for readability only.

**4.4 Allowance hygiene.** Prefer **exact-amount, short-lived approvals** over unbounded MAX approvals; check `allowance(symbol, spender)` first, `revoke_approval(symbol, spender)` when done. Do not leave standing infinite approvals across sessions.

**4.5 Nonce / concurrency.** One `PrimeDelta` instance owns one signer and **serializes** transactions (each send waits for its receipt; local nonce manager with "nonce too low" retry). Do **not** fire concurrent txs from the same key or two instances sharing one key — you will collide nonces (the nonce counter is not thread-safe). For parallelism use separate, independently KYC'd identities, one in-flight tx per key.

**4.6 Error handling.** Catch explicitly:
- `TransactionFailed` — on-chain revert / failed mine. Attributes `.reason` (decoded `Error(string)`/`Panic`), `.tx_hash`, `.to`, `.data` (replay with `cast call`), `.trace`. Selector `0x19abf40e` = stale/absent oracle price → market closed.
- `AccountNotVerified` (DID/KYC gate), `NotEnoughFunds` (`INSUFFICIENT_FUNDS`), `NotLoggedIn` (re-`login()`), and config gaps `WdelNotConfigured` / `PoolNotFound` / `RouterNotConfigured` / `QuoterNotConfigured` / `PositionManagerNotConfigured` — not transient. Do not blind-retry a deterministic revert.

**4.7 Idempotency.** Methods return a **tx hash** or a **server-side id** (`order_id`, `withdrawal_id`). The SDK does **not** dedupe backend requests — before retrying a call that may have partially succeeded, check state first (`get_order_status`, `open_orders`, `claimable_withdrawals`, `get_onchain_*_balance`). Persist submitted hashes/ids; reconcile on restart. Deposits/withdrawals are two-phase (`request_*` → `claim_*`) — treat the id as the idempotency key.

**4.8 Reads vs writes.** `quote_swap`, `spot_price`, `preview_fees` (your own positions only), on-chain balance reads, and DID reads are cheap and read-only — use them liberally before writing. Prefer `get_onchain_*_balance` right after a swap; the backend portfolio indexer lags.

**4.9 Networks.** Never point a mainnet key at a dev/testnet config or vice-versa. `network=` selects the whole stack. Verify `network` and the resolved chain match your intent before the first write.

## 5. Fair use & anti-abuse policy
PrimeDelta welcomes automated, legitimate trading agents and does **not** tolerate market abuse. PrimeDelta is a KYC-gated venue for tokenized equities. By trading through this API you accept:

- **Identity is mandatory and revocable.** Every equity and dUSD movement is gated on-chain by your Digital Identity (DID). We can invalidate a DID, which immediately reverts all of that identity's transfers, swaps, and LP actions, and we can block an account so pending withdrawals cancel and no new orders are accepted. Operating multiple identities to evade limits (Sybil) is grounds for invalidation of all linked identities.
- **Prices are bounded, expiring, and market-hours-only.** Signed equity prices are withheld when they deviate beyond set tolerances, are stale, or fall outside US market hours; the on-chain oracle rejects stale, future-dated, or non-monotonic price updates. A swap built on a withheld or expired price reverts. There is no valid way to trade an equity pool against a stale or off-hours price — the attempt simply fails.
- **Self-dealing is refused.** An order that crosses your own opposite-side resting order is rejected.
- **Vouchers are single-use.** Mint/burn/withdrawal authorizations are nonce-bound and cannot be replayed.
- **Conduct is logged, reviewed, and acted on.** Trading activity is logged and subject to review. Accounts and identities involved in wash trading, quote-stuffing, oracle-timing abuse, or attempts to evade market-hours or KYC controls will be blocked and their DIDs invalidated. Repeat or automated abuse results in permanent removal.

**Worked example — why latency arbitrage does not work here.** On many venues a fast trader acts on a price update before liquidity providers reprice, capturing the difference. PrimeDelta is designed so that edge does not exist for equity pools: signed prices carry a short expiry and a strict "newer-than-last" rule, are withheld entirely outside market hours and when they move beyond deviation bounds, and the on-chain oracle rejects any price that is stale or future-dated. In practice there is no window in which you hold a fresh, tradeable price that liquidity providers do not also have — an attempt to exploit one reverts rather than fills. Strategies whose profit depends on speed against the price feed are non-functional and treated as abuse: the identities behind them are subject to blocking and DID invalidation.

**If you are a legitimate agent:** quote before you trade, respect market hours and slippage bounds, keep one in-flight tx per key, and back off on errors instead of hammering. You will not trip these controls. If a control appears to misfire, stop and surface it to the operator rather than routing around it.
