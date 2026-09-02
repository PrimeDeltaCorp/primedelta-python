# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project aims to follow
semantic versioning once published.

## [Unreleased]

### Fixed
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

### Changed
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

### Fixed
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

### Added
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

### Changed
- **Network calls now time out.** Every backend HTTP request (via a
  `requests.Session` subclass) and every JSON-RPC call (web3 `HTTPProvider`)
  carries a default 30s timeout, so a hung node or backend can no longer stall a
  caller or a background task indefinitely. Long-lived SSE price streams are
  exempt, and a per-call `timeout=` still overrides.

### Fixed
- **`craft()` now works for every action that passes a struct.** Calls that
  pass a Solidity struct as a dict — LP `mint` / `increaseLiquidity` / `collect`,
  and factory `burnStablecoin` / `mintStablecoin` / `burnStocks` / `mintStocks`
  and DigitalIdentity `mint` (deposits, withdrawal claims, identity mint) —
  encode fine when broadcasting but `craft()`'s low-level offline encoder rejects
  a dict, so crafting them raised "could not encode calldata". Fixed centrally:
  the craft encode path now falls back to `Contract.encode_abi`, which aligns a
  dict to its ABI tuple. Call sites keep their readable dict structs; the
  broadcast path is unchanged.

### Added
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

### Testing
- Client transport unit coverage (CSRF/cookie, 204/empty-body, error mapping,
  parsing) and a live-contract-drift guard (auth contract + endpoint shapes +
  bundled-ABI-vs-on-chain selectors) that runs against dev on a schedule.

### Notes
- The `mainnet` config is intentionally not shipped yet — the documented
  addresses are not resolvable on the public mainnet RPC read path.
- License is unchanged pending the relicense decision; not a build blocker.
