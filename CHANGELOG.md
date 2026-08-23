# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project aims to follow
semantic versioning once published.

## [Unreleased]

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
