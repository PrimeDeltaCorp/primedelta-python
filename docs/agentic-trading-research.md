# Agentic Trading — SDK Readiness, AGENTS.md & MCP (research, 2026-08-20)

> Output of the `agentic-trading-research` ultracode workflow (6 agents, code-grounded). Decision report.

# PrimeDelta Python SDK — Agentic Trading Readiness: Synthesis & Decision Report

Sources reconciled: R1 (capability inventory, ground truth), R2 (agentic gaps), R3 (AGENTS.md), R4 (MCP), R5 (market-integrity code audit). Where two inputs disagree, R5's code-cited findings and R1's public-surface inventory win over R2/R3/R4 assertions, and the conflict is flagged inline.

---

## 1. Agentic-trading readiness — verdict

**Overall: PARTIAL. Ready for one lane today; not ready for the other without SDK work.** The SDK is a correct, well-typed *human-client* wrapper (revert decoding, nonce-lag retry, PoA middleware, KMS signing, cross-dex routing all work). It is not yet a *decision-safety* layer: no trade simulation, no idempotency, no pre-send market-closed guard, no quote coverage for the oracle instruments, polling-only feedback, no risk rails.

### By strategy class

| Strategy class | Verdict | Why |
|---|---|---|
| **AMM 24/7 tokens (AMMT1, AMMT2, WDEL)** | **YES, cautiously** | `quote_swap`/`spot_price` resolve the V3 pool and work; slippage args (`min_amount_out`/`max_amount_in`) are enforceable; no market-hours trap; `deadline_seconds` present. Safe *if* the agent quotes first, computes its own min-out, sets its own HTTP/receipt timeouts, and serializes sends per instance. |
| **Oracle / market-hours stocks (AAPL-class, PRICE_FEED pools)** | **NO** | Three compounding blockers: (a) no pre-trade quote — `quote_swap`/`spot_price` call `getPool(stock,dUSD,3000)` which returns zero for PRICE_FEED stocks → `PoolNotFound`; (b) no pre-send market-closed/staleness guard — off-hours the backend returns empty signed prices and the pool reverts `0x19abf40e` (`FIOracle.StalePrice`, per R5) with no typed exception; (c) the signed oracle price that determines the fill is never decoded to a number. An unattended agent silently burns gas on reverts and trades without a price estimate. |
| **Custodial brokerage orders (limit/market via backend)** | **PARTIAL** | Usable for slow, supervised strategies; unsafe autonomously — no idempotency keys on order POSTs, polling-only feedback, invisible partial fills (`Order` has no filled/remaining field), and no market-BUY method (buys are limit-only). |

### Top capabilities present (do not rebuild these)
- Rich on-chain error handling: `TransactionFailed` carries `.reason` (decoded `Error(string)`/`Panic`), `.tx_hash`, `.to`, `.data`, `.trace`.
- Sequential nonce manager with "nonce too low" retry/backoff for laggy Besu (`_reserve_nonce`, `_send_with_nonce_retry`).
- Clean `Signer` seam: `LocalAccountSigner`, `KmsSigner` (key never leaves KMS), `BrowserSigner`.
- Slippage/deadline params exist and flow to the router; AMM add/remove enforce `amount*_min`.
- On-chain balance reads that bypass indexer lag (`get_onchain_*_balance`).

### Prioritized gap roadmap

| # | Gap (what) | Severity | Effort | Concrete SDK change |
|---|---|---|---|---|
| 1 | **No trade simulation** — cannot answer "what will I get?" without spending gas | BLOCKER | M | Add `preview_swap(...)` returning expected out + effective price via static `call({"from":...})`, reusing the existing `preview_fees` pattern (`handlers.py:684`). |
| 2 | **No market-closed / empty-signed-price guard** | BLOCKER | S | Detect empty `update_data` before send, map `0x19abf40e` in `_decode_revert`, add `instrument_kind(symbol)` classifier (AMM 24/7 vs oracle-gated), optionally auto-check `is_market_open()` on oracle swaps. Highest value-per-effort item. |
| 3 | **No quote/price coverage for oracle stocks** | BLOCKER (oracle lane only) | M | Decode the signed-price bytes from `/signed-prices/` into a `Decimal` and/or add an oracle-pool quote path so AAPL-class trades can be priced. |
| 4 | **No idempotency** on order/withdrawal/fiat POSTs | MAJOR | M (needs backend) | Client-generated idempotency key echoed by backend; add a `retryable` flag on exceptions. On-chain vouchers are already nonce-protected; the *backend request* that mints them is not. |
| 5 | **Slippage foot-gun** — docs ship `min_amount_out=Decimal("0")` | MAJOR | S | Add `min_out_from_quote(quote, slippage_bps)`; stop shipping `0` in examples/README. |
| 6 | **No timeouts; fragile streams** | MAJOR | M | HTTP timeout on the requests session; `timeout=` on `wait_for_transaction_receipt`; SSE auto-reconnect + idle-heartbeat + staleness flag on `Price`. |
| 7 | **No risk rails; nonce not thread-safe; signer mandatory for read-only** | MAJOR | S–M | `max_notional` ceiling, `kill_switch`/halt flag, lock around `self._next_nonce`, opt-in read-only construction (no signer for `quote_swap`/`spot_price`/`stocks`). |
| 8 | **Weak execution feedback** — polling only, partial fills invisible, no market-buy | MAJOR | L | Expose filled/remaining on `Order`, async submit-and-watch (confirmations), add market-buy for surface symmetry; longer term an order/fill event stream. |

Items 1–3 gate the oracle lane. Items 5, 2, 7 are cheap and unblock safer AMM-lane autonomy immediately.

---

## 2. AGENTS.md — recommendation + ready draft

**Recommendation: SHIP IT.** It is worth shipping *now* precisely because the SDK does not yet enforce the safe pattern (quote → min-out → simulate → check-market → send) — the file hard-codes the discipline the code is missing. Adapt the convention: this is a **runtime operating handbook** for an agent that `import primedelta`s and trades, not a codebase-editing guide.

**Where it lives / how agents consume it:**
- `/AGENTS.md` at the `primedelta-python` repo root (auto-discovered by Claude Code, Codex, Cursor, etc., closest-file-wins).
- Ship it **inside the pip package** too (`[tool.setuptools.package-data]` / MANIFEST) so pip-install agents get it without cloning.
- Reuse the identical text as the future MCP server `instructions` resource / system-prompt preamble — one source of truth.
- State plainly in the file that it is advisory to the agent; real enforcement is server-side and on-chain.

**Correction applied to R3's draft (contradiction resolved):** R3's fair-use section claimed the signed-price fetch is "rate-limited" and that abuse patterns are "logged and flagged." R5's code audit contradicts both: the signed-price / swappable-symbols endpoints are plain Django `@require_safe` views that **bypass DRF throttling** (§2h GAP), and integrity events are **log lines only — no metric, no alert, no auto-ban** (§2i GAP). The fair-use section below is reworded to R5's accurate posture: **"logged, reviewed, and acted on,"** never "automatically detected in real time," and the rate-limiting claim on the price feed is removed. I also stripped numeric deviation percentages / staleness windows / polling cadence per R5's 3c (those are exploit tuning-knobs).

**One flagged judgment call:** R5 advises against printing the `0x19abf40e` selector in agent-facing docs. I kept it *only* in the error-handling rule (§4.6) as a practical revert-mapping aid, because it is already public — the `dex-frontend/src/swapRouter.ts` maps it — so omitting it is low-value obscurity. It is kept **out** of the fair-use section. If the team prefers strict adherence to R5, delete the one `0x19abf40e` mention in §4.6.


_(The full drop-in handbook was extracted to [`/AGENTS.md`](../AGENTS.md).)_


---

## 3. MCP server — GO/NO-GO + plan

**Recommendation: CONDITIONAL GO, read-only first.** MCP adds nothing for someone who can already `import primedelta` (quants should use the SDK directly). Its value is making the surface consumable by **chat/agent hosts that cannot import Python** — retail in Claude Desktop ("show my AAPL P&L and spot price"), and internal ops/support behind SSO. Protocol/SDK risk is low: the official Python SDK is GA and FastMCP gives elicitation/confirmation for free.

### Tool surface (thin wrappers over the `primedelta` facade)
- **P0 — READ-ONLY (ship first, most need no signer):** `pd_account_status`, `pd_portfolio`, `pd_portfolio_history`, `pd_balances`, `pd_stocks`, `pd_swappable_symbols`, `pd_quote`, `pd_spot_price`, `pd_market_open`, `pd_limit_buy_cost`/`pd_limit_sell_cost`/`pd_market_sell_cost`, `pd_open_orders`/`pd_closed_orders`/`pd_order_status`, `pd_lp_positions`/`pd_lp_position`/`pd_preview_fees`, `pd_allowance`, `pd_claimable_withdrawals`/`pd_pending_transfers`/`pd_closed_transfers`/`pd_distributions`, `pd_messages`. Expose `prices_stream`/`pyth_prices_stream` as **MCP Resources**, not tools.
- **P1 — STATE-CHANGING (gated):** `pd_login`, `pd_place_limit_order`, `pd_sell_market_order`, `pd_cancel_order`, `pd_swap_exact_input`/`_output`, `pd_swap_token_to_token_*`, `pd_approve`/`pd_revoke_approval`, `pd_wrap_del`/`pd_unwrap_del`, `pd_send_del`, `pd_deposit_*`, `pd_request_/claim_*_withdrawal`, `pd_add_liquidity`/`pd_increase`/`pd_remove`/`pd_collect_fees`/`pd_burn_position`, `pd_request_fiat_withdrawal` (off-ramp, **disabled by default**), `pd_claim_digital_identity`.

### Key-custody / safety model (the core risk)
- **The LLM must never see the key; the raw key never crosses a network boundary.** The signer is constructed **out-of-band** at process start from local env/keystore/KMS — **never a tool argument, never a model-visible value.**
- **Local stdio is the default** (P0/P1): process runs on the user's machine, signs in-process, exactly like the SDK today.
- **KMS (`KmsSigner`)** for headless/server: process holds no key material.
- **Browser-bridge (`BrowserSigner`)** for strongest retail human-in-the-loop.
- **Two independent auth layers, don't conflate:** host↔MCP (stdio trust, or OAuth 2.1+PKCE with `aud` validation for hosted) and SDK↔backend (SIWE cookie via `pd_login`). The on-chain `DID_MINTED` gate is a free backstop the MCP layer inherits.
- **Never** expose any tool that accepts or returns a private key.

### MCP-layer policy engine (enforce before delegating to the SDK; SDK stays policy-free)
Read-only by default (write tools not registered unless `PRIMEDELTA_MCP_ALLOW_TRADING=1`); `dry_run=true` default on every write; elicitation confirm echoing symbol/side/amount/min-out/notional (URL mode for anything credential-like); max-notional per-tx + rolling caps; slippage floor (refuse `min_amount_out=0`); market-hours guard on oracle tools surfacing the closed-market revert as a friendly precondition; approvals capped to exact amount (never MAX_UINT); recipient allowlist on `pd_send_del`; per-session rate limits + full audit log; pinned/versioned tool descriptions (anti tool-poisoning); the same fair-use policy text as AGENTS.md in tool descriptions and a server Prompt.

### Architecture & packaging
Thin FastMCP server, one `PrimeDelta` per session, each tool ~5 lines (validate/cap → call SDK → serialize dataclass to JSON). **Separate repo `primedelta-mcp`** (not a base extra) — the SDK's dep set is tight (`web3/siwe/requests/sseclient`); MCP pulls a heavier stack, and a separate repo versions independently and carries its own security review. Depends on `primedelta>=<first-published>`.

### Phased plan
| Phase | Scope | Effort |
|---|---|---|
| **P0 read-only** | All read tools + price Resources; stdio; dry-run harness; audit-log skeleton; Claude Desktop config sample. | ~1–1.5 wk |
| **P1 gated trading (local)** | Write tools behind `ALLOW_TRADING`; dry-run default; elicitation confirm; policy engine (max-notional/slippage/market-hours/approval caps) unit-tested with mutation coverage; KMS + browser-bridge signer paths. | ~2–3 wk |
| **P2 hosted / multi-tenant** | Streamable HTTP + OAuth 2.1/PKCE, `aud` validation, per-tenant scopes; **no server-side key custody** (per-tenant KMS grant / browser session); rate limits + security review. | ~3–5 wk + review |

**Conditions:** GO now on P0. P1 only when all hold — policy engine mutation-tested; signer provably out-of-band; dry-run + elicitation on every write; shared anti-abuse text + audit logging wired; cold security review of the write path. **NO-GO on P2 as a shared-custody service** (holding many users' keys is the "send your key to a remote server" anti-pattern) — P2 is allowed only as OAuth orchestration where each tenant's keys stay in their own KMS/browser.

---

## 4. Anti-abuse grounding

R5's code audit is the authority here; it corrects R2/R3/R4 where they over-claimed. **What we can honestly say we enforce vs. what would make "detect & ban" real:**

### Enforced today (code-cited, EXISTS)
- **DID/KYC gating with a revocable kill switch** — every tokenized-equity + dUSD movement gated on-chain (`Stock.sol:99,183` `isValid(getId(to))`/`verifyTransfer` → `InvalidDID`; `Factory.sol` vouchers; `DclexPool.sol:192,650,662` LP exits). Admin `setValid(id,false)` (`DigitalIdentity.sol:106`) invalidates a DID → all its stock transfers/swaps/LP revert. Hardest on-chain ban. **Caveat: AMMT/WDEL are not tokenized equities → not DID-gated (permissionless, 24/7).**
- **Signed-price deviation guard (audit F-014), fail-closed** — `price_deviation_guard.py evaluate()` withholds on `NON_POSITIVE/POST_DATED/CLOSE_DEVIATION/TICK_DEVIATION/NO_REFERENCE/BOOTSTRAP_BAND`; withheld symbol omitted from feed → on-chain swap reverts. Reference is last *signed* price (a withheld tick can't poison the band).
- **Market-hours gating** — `signed_prices_endpoint.py:179 _market_is_open()`; both signed-prices and swappable-symbols return `[]` when closed; halted symbols dropped; order side has holiday/early-close calendar.
- **Price staleness / expiry / replay, belt-and-suspenders** — backend refuses stale + post-dated quotes and clamps `publishTime`; `FIOracle.sol` rejects future-dated (`MAX_CLOCK_SKEW`), stale (`StalePrice`), and non-monotonic (`publishTime <= last` skipped) prices; signature must recover to `trustedSigner`; `DclexPool.sol` has `MAX_PRICE_STALENESS`, deadline, and slippage bounds. **This is the real teeth against latency/stale arbitrage.** (`0x19abf40e` = `FIOracle.StalePrice`.)
- **Single-use vouchers** — permanent used-nonce sets + `InvalidNonce` (`Factory.sol`, `DigitalIdentity.sol`); EIP-712 binds symbol/amount/account/consumer.
- **Wash-sale block — narrow** — `orders_app.py:251 WashSaleProhibitedError` refuses a limit order crossing *your own* opposite-side order at the *same price*. Single-account, same-price, limit-only.
- **Block/suspend enforcement — but MANUAL** — `User.is_blocked`, `block_user()` → cancels pending withdrawals + blocks order placement. Triggered by an **admin command with OTP step-up**; `SuspiciousUser` is a watch-list annotation only.

### Gaps that must close before "detect & ban" is literally true
1. **Rate-limit the signed-price / swappable endpoints.** They are plain Django `@require_safe` views that **bypass DRF throttling** — the exact surface a latency-arb / quote-stuffing bot polls hardest is ungoverned at the app layer. *Lowest effort, closes the most-abused surface.* (This directly contradicts R3's original "signed-price fetch is rate-limited" claim — removed.)
2. **Automated anomaly signals feeding `SuspiciousUser`** — a job flagging N swaps within one price-update window per DID, repeated deviation-guard withholds against one address, cross-account round-trips. Turns "manual review" into "reviewed with leads."
3. **Metrics + alerting on integrity events** — today withholds and cross-source divergence are `logger.warning/error` **log lines only**, no metric/alert/auto-ban.
4. **Cross-account / Sybil wash detection** — current wash-sale check is single-account/same-price/limit-only; needs correlation across accounts sharing funding/withdrawal addresses/timing + extension to market orders.
5. **Document/enforce MEV posture** — front-running is *structurally* limited (permissioned IBFT validator set, oracle-priced equity pools) but not guaranteed in code.

### Accurate disclaimer language
Use **"logged, reviewed, and acted on"** — **never "automatically detected in real time."** We may claim: hard on-chain KYC gating with revocable kill switch, single-use vouchers, market-hours + deviation + staleness withholding, monotonic non-replayable oracle, single-account wash-sale block, manual block/suspend enforcement. We must **not** claim (until gaps 1–3 land): automated real-time abuse detection, cross-account/Sybil wash detection, a rate-limited price feed, or MEV prevention. Keep numeric windows, deviation percentages, polling cadence, and revert selectors out of agent-facing docs.

---

## 5. Concrete next steps (ordered)

**Quick wins (days):**
1. **Ship AGENTS.md** (Section 2 draft) at repo root + in the pip package. Zero code. Highest immediate leverage — it hard-codes the safe pattern the SDK doesn't enforce. *(Ready to drop in.)*
2. **SDK gap #2 — market-closed guard + `instrument_kind(symbol)` classifier + map `0x19abf40e` → typed `MarketClosed`.** Small, unblocks safe oracle-lane error handling. *(BLOCKER, S.)*
3. **SDK gap #5 — `min_out_from_quote(quote, slippage_bps)` helper; purge `Decimal("0")` from README/examples.** *(MAJOR, S.)*
4. **SDK gap #7 (partial) — read-only construction (no signer), nonce lock, `kill_switch` + `max_notional`.** *(MAJOR, S–M.)*
5. **Anti-abuse gap #1 — rate-limit the signed-price / swappable endpoints** (Django middleware or ALB `limit_req`). Backend change, small, closes the most-abused surface and makes the disclaimer's implicit "we can throttle you" true. *(Backend, S.)*

**Larger efforts (weeks):**
6. **SDK gap #1 — `preview_swap()` static-call simulation** (reuse `preview_fees` pattern). *(BLOCKER for autonomous confidence, M.)*
7. **SDK gap #3 — decode signed-price bytes to a `Decimal` / oracle-pool quote** so the oracle lane can be priced. *(BLOCKER for oracle lane, M.)*
8. **Build `primedelta-mcp` P0 (read-only), separate repo.** ~1–1.5 wk, near-zero custody risk, genuine value for chat/ops. Then P1 only under the five conditions in §3.
9. **SDK gap #6 — HTTP/receipt timeouts + resilient SSE (reconnect, heartbeat, staleness flag).** *(MAJOR, M.)*
10. **SDK gap #4 — idempotency keys on order/withdrawal POSTs + `retryable` flag** (needs backend coordination). *(MAJOR, M.)*
11. **Anti-abuse gaps #2–3 — anomaly job feeding `SuspiciousUser` + Prometheus counters/alerts on integrity events.** Required before upgrading the disclaimer to any real-time-detection wording. *(Backend, M.)*

**Sequencing logic:** steps 1–5 are cheap and make the AMM lane safely autonomous and the anti-abuse posture honestly enforceable *now*. Steps 6–7 unblock the oracle lane. Step 8 (MCP P0) can run in parallel with any of the above since it only wraps existing read methods. Steps 10–11 need backend coordination and are the long poles.

**Note on input quality:** all five inputs were substantive and code-grounded. The one material contradiction was R3's fair-use claim that the signed-price feed is rate-limited and abuse is "flagged" — R5's code audit disproves both; the draft and disclaimer above are corrected to R5. R2's oracle-lane blockers, R1's inventory, R4's MCP custody model, and R5's enforcement citations are mutually consistent.