# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project aims to follow
semantic versioning once published.

## [Unreleased]

### Added
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

### Testing
- Client transport unit coverage (CSRF/cookie, 204/empty-body, error mapping,
  parsing) and a live-contract-drift guard (auth contract + endpoint shapes +
  bundled-ABI-vs-on-chain selectors) that runs against dev on a schedule.

### Notes
- The `mainnet` config is intentionally not shipped yet — the documented
  addresses are not resolvable on the public mainnet RPC read path.
- Relicensed under the **MIT License** (was the non-commercial license) —
  publication approved.
