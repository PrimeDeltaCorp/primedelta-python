# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project aims to follow
semantic versioning once published.

## [Unreleased]

### Added
- **Every on-chain write is simulated before it is signed.** The SDK runs an
  `eth_call` of the exact transaction, pinned to the block that holds this
  client's last write, before it signs it or hands it to a wallet. A revert
  raises `TransactionFailed` (`MarketClosed` for a stale oracle price) with
  `tx_hash=None`: nothing is broadcast, no gas is spent, and a browser wallet is
  never asked to approve a transaction that would fail. A transport error skips
  the check, since the chain still enforces it. Turn it off with
  `PrimeDelta(preflight=False)` or `PRIMEDELTA_PREFLIGHT=0`. `craft()` never
  simulates.
- **`BrowserSigner` keeps one wallet tab per session.** Its loopback server now
  lives as long as the signer, and the page it opens stays open and long-polls
  for the next request, so the wallet asks to connect once per session and
  every later signature or transaction appears in that tab instead of a new
  one. A new tab opens only when none is polling. The page refuses a request
  for an account other than the one the session connected. `port=` (or
  `PRIMEDELTA_BROWSER_SIGNER_PORT`) pins the loopback port so the wallet
  remembers the connection across restarts; a busy pinned port falls back to a
  random one. `loopback_origin` returns the page origin and `close()` stops the
  server. A time-out now says whether the wallet ever received the request
  ("nothing was sent") or may still complete it ("check balances and
  transactions before retrying").
- **Loopback sign-in on dev.** A `BrowserSigner` signs in with the SIWE domain
  `127.0.0.1` and its loopback origin as the URI on networks whose backend
  accepts it (only `dev` today), so MetaMask no longer marks the sign-in as
  suspicious. `PRIMEDELTA_SIWE_LOOPBACK=1` / `0` forces it on or off; setting
  `PRIMEDELTA_BASE_URL`, `PRIMEDELTA_APP_URL` or `PRIMEDELTA_SIWE_DOMAIN` turns
  it off. Needs https://github.com/PrimeDeltaCorp/gitops/pull/438 deployed.
- **AI-agent management for a main account.** `get_my_ai_agents()` lists every
  AI agent linked to the main as `AIAgent` (`sub_wallet_address`, `agent_name`,
  `status` as an `AccountStatus`, with an unknown status kept on `raw_status`).
  `fund_ai_agent(sub_wallet_address, amount)` moves USD from the main's ledger
  to an agent; `return_to_main(amount, sub_wallet_address=None)` moves it back,
  called by the agent itself or by the main naming the agent. Both return an
  `InternalTransfer` (`transfer_id`, `kind` `FUND` / `RETURN`, `amount`,
  `symbol`, `from_wallet_address`, `to_wallet_address`, `created_at`) and take a
  `request_id`: repeating one returns the original transfer instead of moving
  the funds again. A fresh UUID4 is sent when it is omitted, so pass your own
  and reuse it to retry safely. Both raise `CannotCraft` inside `craft`.
- **`request_ai_agent_approval(sub_wallet_address, agent_name=None)`** returns
  an `AIAgentApproval` (`nonce`, `expires_at`, `main_message`, `agent_message`):
  a single-use approval valid for 10 minutes. `agent_name` is required unless
  the wallet is already awaiting this main's confirmation.
- **`link_ai_agent(sub_wallet_address, approval, agent_signature)`** links and
  confirms an agent wallet that never logs in. The agent wallet signs
  `approval.agent_message` (EIP-191 `personal_sign`) wherever its key lives, and
  this client's signer signs `approval.main_message`.
- **Typed AI-agent errors.** `AIAgentError` (an `APIError`) and its subclasses
  `AIAgentApprovalError` (`APPROVAL_SIGNATURE_REQUIRED`, `INVALID_APPROVAL`,
  `APPROVAL_EXPIRED`, `INVALID_APPROVAL_SIGNATURE`) and `AIAgentTransferError`
  (`NOT_A_MAIN_ACCOUNT`, `AGENT_NOT_FOUND`, `AGENT_ADDRESS_REQUIRED`,
  `ACCOUNT_NOT_ACTIVE`, `INVALID_AMOUNT`, `REQUEST_ID_CONFLICT`) are raised by
  every AI-agent method, including `register_ai_account` and
  `reject_ai_agent`; the link/confirm codes (`SUBACCOUNT_NOT_FOUND`,
  `SUBACCOUNT_LIMIT_REACHED`, …) raise `AIAgentError` itself. The backend sends
  only the code, so the SDK supplies a short reason. `INSUFFICIENT_FUNDS` on a
  transfer raises `NotEnoughFunds`. Existing `except APIError` handlers still
  catch them.
- **A main account reads its agent.** `get_ai_agent_portfolio(sub_wallet_address)`
  returns a `Portfolio`; `get_ai_agent_open_orders(sub_wallet_address)` /
  `get_ai_agent_closed_orders(sub_wallet_address)` return `Order`s (same
  paging as `open_orders` / `closed_orders`). Needs
  https://github.com/PrimeDeltaCorp/backend/pull/405.
- **`close_ai_agent(sub_wallet_address)` / `reopen_ai_agent(sub_wallet_address)`.**
  Closing invalidates the agent's digital identity (DID-gated tokens on its
  wallet freeze), cancels its open Mint orders and moves its Mint balances to
  the main; repeating it is harmless. Reopening makes the DID valid again;
  swept balances stay on the main. `AccountStatus.CLOSED` is the closed
  agent's status, and the DID/KYC checks treat it as not verified.
  `close_ai_agent` raises `CannotCraft` inside `craft`. Needs
  https://github.com/PrimeDeltaCorp/backend/pull/406.
- **Agent trading policy.** `get_ai_agent_policy(sub_wallet_address=None)` —
  a main names its agent, an agent reads its own — and
  `set_ai_agent_policy(sub_wallet_address, *, paused, allowed_symbols,
  max_order_usd, max_daily_usd)` return an `AIAgentPolicy` (the rules plus
  today's `used_today_usd`, `remaining_today_usd` and `resets_at`). Setting
  replaces the whole policy: every rule is a required keyword and `None`
  turns it off; the caps take `Decimal`/`int` > 0 with at most 2 decimal
  places, checked before the request. The allow-list and the caps apply to
  buy orders; `paused` blocks every new order. `AIAgent` from
  `get_my_ai_agents()` gains `paused`, `allowed_symbols`, `max_order_usd` and
  `max_daily_usd` (all `None` on a backend without policies). Needs
  https://github.com/PrimeDeltaCorp/backend/pull/407.
- **`AIAgentPolicyError`** (an `AIAgentError`) — `send_limit_order` /
  `send_sell_market_order` raise it when an AI agent's order breaks its
  policy: `AGENT_PAUSED`, `AGENT_SYMBOL_NOT_ALLOWED` (`allowed_symbols`),
  `AGENT_ORDER_LIMIT_EXCEEDED` (`limit_usd`, `order_value_usd`) or
  `AGENT_DAILY_LIMIT_EXCEEDED` (`limit_usd`, `remaining_usd`,
  `order_value_usd`), with the reason spelled out from those fields. A
  `AGENT_PAUSED` 403 is no longer retried as a stale CSRF token.
- **`APIError.payload`** — the whole error body, so a refusal's extra fields
  survive.
- **`TransactionType.INTERNAL_OUT` / `TransactionType.INTERNAL_IN`** — cash
  (USD) moves between a main account and its AI agent, written by the backend
  as settled rows, so they show up in `closed_transfers()`. They used to parse
  as `UNKNOWN` (value on `raw_type`); they now parse as their own members and,
  like the `FIAT_*` rows, carry symbol `cash` and a USD amount.

### Changed
- **One read resolves every token symbol.** The first lookup of a symbol reads
  `symbol()` of every token the router lists in one Multicall3 `aggregate3` call
  and caches all of them, instead of one `eth_call` per token for each new
  symbol. Without Multicall3, or when the batch fails, it falls back to the
  per-token scan.
- **Oracle-priced liquidity skips an approve that is already right.**
  `add_liquidity(PriceFeedAddLiquidity)` still sets each pool allowance to
  exactly `max_stock_amount` / `max_stablecoin_amount`, the on-chain bound on
  what the pool may pull, but no longer re-sends an `approve` whose allowance
  already equals that cap, for example when retrying an add that reverted.
- **`confirm_ai_agent()` signs the approval.** It requests an approval, signs
  its `main_message` with the client's signer (EIP-191 `personal_sign`, so a
  browser wallet shows the text to the user) and confirms with the nonce and
  signature. It keeps working once the platform requires a signed approval.
  Every bundled signer can sign a message (login needs it too), so there is no
  unsigned fallback. A wallet that is not awaiting this main's confirmation now
  fails at the approval step with `AGENT_NAME_REQUIRED` instead of
  `SUBACCOUNT_NOT_FOUND` / `SUBACCOUNT_NOT_AWAITING_CONFIRMATION`.
- An AI-agent refusal's class follows the call: `fund_ai_agent` /
  `return_to_main` raise `AIAgentTransferError`, the other agent calls
  `AIAgentError`, and an approval code `AIAgentApprovalError` everywhere.

### Fixed
- **`stocks()` reads every page.** It used to stop after the first 100 stocks.
- **`get_account_status()` no longer raises `ValueError` on a status the SDK
  does not know.** `SUBACCOUNT_REJECTED` (an AI subaccount whose main account
  rejected it) is now `AccountStatus.SUBACCOUNT_REJECTED`, and any other new
  value returns the new `AccountStatus.UNKNOWN`. Before, a rejected agent got
  `ValueError` from `logged_in()`, `prices_stream()` and every DID/KYC check.
  Both now count as not verified, so `claim_digital_identity()` and the other
  DID/KYC checks raise `AccountNotVerified`.

## [0.2.0] - 2026-10-03

### Added
- **Transfer, distribution and closed-order rows with a type or status the SDK
  does not know yet stay readable.** `pending_transfers()` /
  `closed_transfers()`, `distributions()` and `closed_orders()` raised
  `ValueError` for the whole page when one row carried such a value (e.g. a
  transfer type the backend added later). That row now parses as the new
  `UNKNOWN` member of `TransactionType` / `TransferHistoryStatus` /
  `DistributionType` / `OrderStatus`, and the backend's value is kept on the new
  `Transfer.raw_type` / `Transfer.raw_status`, `Distribution.raw_type` and
  `Order.raw_status` — `None` whenever the SDK knows the value.
- **`Order.filled_quantity`** — how much of the order has filled so far, parsed
  from the `filledQuantity` the backend already sends on `open_orders()` /
  `closed_orders()`. The SDK dropped it, so partial fills were invisible.
- **`APIError.message` / `APIError.detail` keep the backend's reason.** A
  business error's `message` and a field-validation `detail` are kept on the
  exception and included in `str(exc)`, so a rejection such as a quantity finer
  than the instrument allows no longer arrives as a bare `INVALID_REQUEST`.
- **`Stock.quantity_decimals` / `Stock.price_decimals`** (also on portfolio
  `Position`) — how many decimal places the platform accepts in an order
  quantity and price for that stock, parsed from the `quantityDecimals` /
  `priceDecimals` that `stocks()` and `portfolio()` return. Today stocks trade
  in whole shares (`0`) at cent prices (`2`), set per environment; both are
  `None` while a backend does not send them yet.
- **`InvalidOrderInput`** — an `APIError` subclass raised by
  `send_limit_order`, `send_sell_market_order`, `limit_buy_cost`,
  `limit_sell_cost` and `market_sell_cost` when the platform rejects the
  quantity or price (`INVALID_QUANTITY`, `INVALID_QUANTITY_PRECISION`,
  `INVALID_PRICE`, `INVALID_PRICE_PRECISION`). The backend sends only the code,
  so the SDK supplies a short reason (`INVALID_QUANTITY_PRECISION: quantity has
  more decimal places than the stock's quantity_decimals`); a backend message
  wins when present. Existing `except APIError` handlers still catch it.
- **`oracle_price(symbol)` — a reference USD price for oracle (price-feed)
  stocks.** AAPL-class stocks have no on-chain quote (`quote_swap`/`spot_price`
  are AMM-only), so an agent had to bring an out-of-band price. This decodes the
  current signed oracle price (feedId / price / expo from the same signed update
  a swap submits), returning `None` when none is available (e.g. market closed).
  It is a **reference** price, not a fee-adjusted amount-out — the oracle pool
  applies a dynamic reserve-dependent fee, so budget slippage to cover it when
  deriving a swap's `min_amount_out`.
- **`export_session()` / `import_session()` — persist a SIWE login.** Serialize
  the authenticated session cookies and restore them into another `PrimeDelta`
  (e.g. across process restarts) instead of re-running SIWE — useful when the
  login itself needs a wallet approval. `import_session` re-arms auto-relogin, so
  a stale restore surfaces as `NotLoggedIn` and is retried transparently.
- **`on_login` callback** on `PrimeDelta(...)` — fired after every successful
  login, the initial one **and each auto-relogin** (which also runs `login()`).
  Lets a caller re-persist the refreshed session without intercepting the
  internal relogin (so a cache stays fresh instead of holding only the first
  session).
- **`simulate_swap()` — a paper-trade preview of an exact-input swap.** Runs
  entirely on static reads (the on-chain Quoter — no allowance, balance, or
  signature) and returns a `SwapSimulation` (expected output, slippage-bounded
  `min_amount_out` ready to pass to `swap_exact_input`, spot price, pool fee
  tier). AMM pools only, like `quote_swap`.
- **`tx_status(tx_hash)` — look up a transaction's mined receipt.** Returns a
  `TxStatus` (succeeded / block number / gas used), or None if it is not yet
  mined. For polling a hash you hold (e.g. one broadcast externally after
  `craft`, or returned by a prior send).
- **AI-subaccount management** — four client/facade methods for the AI-account
  flow (blockchain#192). `register_ai_account(agent_name, main_wallet_address)`
  is called from the SUBACCOUNT's session (a fresh wallet) to request linking
  under a main; `get_pending_ai_agents()`, `confirm_ai_agent(sub_wallet_address)`
  and `reject_ai_agent(sub_wallet_address)` are called from the MAIN's session to
  list and act on pending requests. A subaccount only becomes active once its
  main confirms it. Returns the new `PendingAIAgent` dataclass.
- **`AccountStatus.AWAITING_MAIN_CONFIRMATION`** — the backend now reports this
  status for an AI subaccount that has registered but not yet been confirmed by
  its main account. Parsing it no longer raises `ValueError` in
  `get_account_status()`; the SDK treats it as not-verified, so
  `claim_digital_identity()` raises `AccountNotVerified` until the main confirms.
- **`RemoteBrowserSigner`** — non-custodial signing for a HOSTED/remote app (an
  MCP server that can't open the user's *local* browser). It reuses
  `BrowserSigner`'s one-shot wallet page and one-time state token, but the
  hosting app serves the page from a public HTTPS origin and delivers the URL via
  a `deliver` callback (e.g. an MCP url-mode elicitation); the app wires
  `GET /sign?state` → `render_page` and `POST /result?state` → `resolve`. The URL
  carries only the opaque token — the tx/message stays server-side — and no
  fund-moving key lives on the server (the user's wallet signs, MetaMask
  extension included). Because that token is a bearer capability, `base_url`
  must be an `https://` origin (a `localhost` origin is allowed only for
  testing).
- **Non-custodial crafting** — `craft(action)` runs an on-chain action (swap,
  LP, native transfer, token approve, custodial deposit/claim) without
  broadcasting and returns the unsigned transaction(s) it would have sent, as
  `{from, to, value, data, chainId}` with gas and nonce left for an external
  wallet to fill. Multi-step actions (e.g. approve → swap) return one dict per
  transaction, in send order. This lets an agent build calldata while the user's
  own wallet holds the key and signs. Backend REST actions (limit/market orders,
  withdrawal requests, order cancels) are not on-chain transactions and raise
  `CannotCraft` under `craft` rather than silently executing for real.
- **Signer abstraction** — `Signer` protocol with `LocalAccountSigner`
  (`from_key` / `from_keystore` / `from_mnemonic`), `KmsSigner` (AWS KMS
  secp256k1, key never leaves the HSM), `BrowserSigner` (loopback bridge to a
  browser wallet via EIP-6963), and `MockBrowserSigner` for CI.
- **Backend surface** — messages, bank details, fiat withdrawal, order/market
  cost previews, swappable symbols, application settings, portfolio history,
  and digital-identity id.
- **dUSD deposit** now routes through the signed `burnStablecoin` voucher so the
  custodial ledger is credited (was a raw vault transfer).
- **On-chain reads/writes** — token allowances (`allowance`/`approve`/
  `revoke_approval`), native `send_del`, on-chain DID reads (`did_token_id`/
  `is_pro`/`is_valid`), V3 quoting (`quote_swap`/`spot_price`), and the V3
  position lifecycle (`increase_liquidity`/`burn_position`/`preview_fees`).
- **Multi-network** — bundled `testnet` config (chain 7357), verified on-chain;
  the config generator now handles both deployment schemas.
- **Typed** — ships `py.typed`.
- **Agent safety rails** — `MarketClosed` (raised when an oracle swap reverts on
  a stale/absent signed price; a `TransactionFailed` subclass), `instrument_kind`
  (`"amm"` 24/7 vs `"oracle"` market-hours), `min_out_from_quote(quote, slippage_bps)`,
  and a `halt()`/`resume()`/`is_halted` kill switch with a per-instance send lock.

### Changed
- **Breaking (0.2.0): `Order.quantity` is a `Decimal`, not an `int`.**
  `open_orders()` / `closed_orders()` passed the backend's quantity through
  `int()`, so `"0.50"` silently became `0` and `"2.75"` became `2`. Quantities
  now keep every decimal place the backend sends. Comparisons with ints still
  work (`Decimal("10") == 10`); code that needs an `int` (`range()`, indexing,
  `json.dumps`) must convert explicitly.
- **Order, cost-preview and stock custody amounts take `Decimal | int`.**
  `send_limit_order`, `send_sell_market_order`, `limit_buy_cost`,
  `limit_sell_cost`, `market_sell_cost`, `deposit_stock_token` and
  `request_stock_withdrawal` check the amount (and `price_limit`) before any
  request: a `float`, `bool` or other type raises `TypeError`; NaN, infinity and
  values `<= 0` raise `ValueError`. Values go on the wire as plain decimal
  strings without an exponent (`Decimal("1E+1")` is sent as `"10"`). The SDK
  does not enforce whole shares: the platform enforces each instrument's
  precision (stocks are whole shares today) and its rejection surfaces as an
  `APIError` carrying the backend's reason.
- **`deposit_stablecoin(amount)` accepts dUSD in cents.** It takes
  `Decimal | int` with at most 2 decimal places (`Decimal("10.50")` is sent as
  `"10.50"`); more decimal places or an amount `<= 0` raise `ValueError` before
  any request. The burn still uses exactly the base-unit amount the backend
  signed. A backend that only takes whole-dollar deposits rejects a fractional
  amount with `APIError` (`INVALID_REQUEST`).
- **Breaking: `request_fiat_withdrawal(amount, bank_account)` now takes the
  beneficiary bank account.** The backend rejects a fiat withdrawal without the
  eight beneficiary fields (HTTP 400), so the amount-only call could no longer
  succeed. Pass a `FiatWithdrawalBankAccount` (beneficiary name/address, bank
  name/address, account number, transit number, institution number, BIC). A
  non-positive amount or a blank field raises `ValueError` before any request;
  other backend rejections surface as `APIError` (`INVALID_WITHDRAWAL_REQUEST`,
  or `INVALID_REQUEST` for field validation such as more than 6 decimal places
  or an over-long field).
- **`Transfer.transfer_id`** carries the backend transfer id (`None` when the
  backend does not send it). One on-chain transaction can produce several
  deposits, so `transaction_id` alone no longer identifies a transfer; use
  `(type, transfer_id)`.
- **Faster `spot_price` / `quote_swap` / swaps on AMM-only tokens.** Resolving an
  AMM-only symbol (AMMT1/AMMT2/WDEL) enumerated `Router.allStockTokens()` and read
  every token's `symbol()` on-chain on EVERY call (~45 reads), and web3 re-fetched
  `eth_chainId` before ~every call — so on a remote RPC a single `spot_price` was
  ~150 round-trips (~15 s). The immutable symbol->address / stock->pool resolution
  is now memoized per network, and `eth_chainId` is cached at the provider. Repeat
  calls drop from ~15 s to ~0.2 s; the first (cold) call ~halves. Live price
  (`slot0`) is still read fresh every call — only immutable plumbing is cached.
  Memoizing on success also stops a flaky gateway read from spuriously raising
  `PoolNotFound` once a symbol has resolved.
- **Network calls now time out.** Every backend HTTP request (via a
  `requests.Session` subclass) and every JSON-RPC call (web3 `HTTPProvider`)
  carries a default 30s timeout, so a hung node or backend can no longer stall a
  caller or a background task indefinitely. Long-lived SSE price streams are
  exempt, and a per-call `timeout=` still overrides.

### Fixed
- **`pending_transfers()` / `closed_transfers()` no longer fail on fiat
  history.** `TransactionType` lacked `FIAT_DEPOSIT` / `FIAT_WITHDRAWAL`, so any
  account with a fiat deposit or withdrawal raised `ValueError` for the whole
  page.
- **On-chain reads now reflect this client's own writes (read-after-write).**
  `get_onchain_stablecoin_balance` / `get_onchain_stock_balance` /
  `get_native_del_balance` and the swap/LP allowance-skip decision are pinned to
  the block that included this client's last send, so an eventually-consistent
  RPC replica can no longer answer with pre-write state — e.g. skipping an
  approve that a just-consumed allowance actually needs (which reverted the next
  swap), or reporting a pre-swap balance. A replica lagging that block is retried,
  then falls back to `latest`.
- **Browser-wallet login was broken by an address-checksum mismatch.**
  `BrowserSigner`/`RemoteBrowserSigner` returned the wallet's address verbatim
  (wallets hand it back lowercased), and `login()` feeds it to `SiweMessage`,
  which requires EIP-55 — so every browser login raised `ValidationError: address
  must be in EIP-55 format`. The signers now checksum the address.
- **BrowserSigner now prints the wallet URL** (stderr) before opening the
  browser, so a user whose default browser has no wallet (e.g. Safari without
  MetaMask → `No EIP-1193 wallet found`) can paste it into the right one, and a
  retry is copy-pasteable; the auto-open is best-effort and no longer the only path.
- **`import primedelta` is quiet.** `siwe` builds an ABNF grammar that redefines
  RFC-5234 core rules (ALPHA/DIGIT/LF/HEXDIG), which `abnf` printed as four
  `GrammarWarning`s on every first import — harmless but noisy. Suppressed around
  the single `siwe` import.
- **Transient backend blips no longer crash the caller.** The HTTP session now
  retries IDEMPOTENT requests (GET/HEAD/OPTIONS) over a dropped/stale-keep-alive
  connection, a read-timeout, or a momentary 5xx, so a blip reconnects instead of
  surfacing a raw `requests` traceback (POST/PUT/PATCH/DELETE are never retried).
  Transport failures that survive the retries, and 5xx responses, now raise a
  typed `BackendUnavailable` (distinct from `NotLoggedIn`/`AuthorizationError`/
  `APIError`) so a caller or the MCP layer can back off cleanly.
- **Fresh installs no longer break on `abnf` 2.9.0.** `siwe==4.4.0` builds an
  ABNF grammar that redefines the `ALPHA` core rule; `abnf` 2.9.0 (released Aug
  2026) turned that from a warning into a fatal `GrammarError`, so a clean
  `pip install` of the SDK failed at `import primedelta`. Pin the transitive dep
  to `abnf<2.9` (2.8.3 works) until `siwe` ships a compatible grammar.
- **Bundled endpoints follow the new hostname scheme.** The infra moved dev/test
  hosts from a `-dev` / `-testnet` suffix to a `.dev` / `.testnet` sub-domain
  (and mainnet to bare, dropping `-mainnet`), so `PrimeDelta(network="dev")`
  without an env override was defaulting to a now-dead backend host. The
  per-network defaults in `resolve_endpoints` are updated to
  `api.dev` / `mint.dev`, `api.testnet` / `mint.testnet`, and bare
  `api.primedelta.io` / `mint.primedelta.io`; docs and examples follow. Env
  overrides (`PRIMEDELTA_BASE_URL` / `PRIMEDELTA_APP_URL`) are unaffected.
- **`craft()` now works for every action that passes a struct.** Calls that
  pass a Solidity struct as a dict — LP `mint` / `increaseLiquidity` / `collect`,
  and factory `burnStablecoin` / `mintStablecoin` / `burnStocks` / `mintStocks`
  and DigitalIdentity `mint` (deposits, withdrawal claims, identity mint) —
  encode fine when broadcasting but `craft()`'s low-level offline encoder rejects
  a dict, so crafting them raised "could not encode calldata". Fixed centrally:
  the craft encode path now falls back to `Contract.encode_abi`, which aligns a
  dict to its ABI tuple. Call sites keep their readable dict structs; the
  broadcast path is unchanged.

### Testing
- Client transport unit coverage (CSRF/cookie, 204/empty-body, error mapping,
  parsing) and a live-contract-drift guard (auth contract + endpoint shapes +
  bundled-ABI-vs-on-chain selectors) that runs against dev on a schedule.

### Notes
- The `mainnet` config is intentionally not shipped yet — the documented
  addresses are not resolvable on the public mainnet RPC read path.
- License is unchanged pending the relicense decision; not a build blocker.
