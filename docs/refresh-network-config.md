# Refreshing the bundled network config after a redeploy

The SDK ships contract addresses locally in `src/primedelta/networks/<network>.json`
(dev, testnet) rather than fetching them from the backend — the backend's
`/contracts/` is wired to GitOps env vars that drift behind redeploys and can
point at stale bytecode. The trade-off: **every chain redeploy changes addresses,
so the bundled JSON must be regenerated.** This is the runbook for that.

## When

After any redeploy that changes contract addresses on a network (new Factory /
Vault / DID / router stack / V3 pools). Symptoms of a stale config: calls revert
or mis-decode, `stocks()` / swaps fail against a fresh chain, or the drift test
(below) fails.

## Regenerate

The blockchain repo records each deploy in
`blockchain/deployments/<env>/addresses.json`. Map it onto the SDK schema with:

```bash
python scripts/generate_network_config.py \
    --addresses /path/to/blockchain/deployments/primedelta-dev/addresses.json \
    --network dev
```

This writes `src/primedelta/networks/dev.json`. Use `--network testnet` with the
testnet deployment file.

### Two deployment schemas

The generator handles both shapes automatically (it tries candidate locations in
order):

| | chain-id key | sections |
| --- | --- | --- |
| dev | `chainId` | `core` / `router` / `v3Main` |
| testnet + mainnet | `chain_id` | `core` / `router_stack` / `v3` |

For `position_manager` the generator reads `DclexPositionManager` from
`router_stack` (canonical, testnet/mainnet) or `v3Main` (dev) — it never reads
the stale `v3.DclexPositionManager_phase3_unused`.

ABIs are **not** regenerated — they come from `src/primedelta/networks/abis/`.
If a contract's ABI changed in the redeploy, update the corresponding file there
too (and the drift test will catch a selector mismatch).

## Verify

1. Diff the JSON and sanity-check the addresses against the deployment doc.
2. Run the live-contract-drift guard against the network — it logs in, checks
   endpoint shapes, and asserts every bundled ABI's selectors are present in the
   deployed bytecode:
   ```bash
   PRIMEDELTA_PROVIDER_URL=https://besu.dev.primedelta.io \
   PRIMEDELTA_TEST_PRIVATE_KEY=0x... \
   pytest tests/integration/test_contract_drift.py -m integration
   ```
   A green run means the refreshed config matches the live chain.
3. Open a PR with the regenerated JSON (and any ABI updates).

## Mainnet

`mainnet.json` is intentionally not shipped yet: the public mainnet RPC serves a
chain state where the documented addresses have no bytecode. Confirm the correct
mainnet RPC/addresses before generating and committing `networks/mainnet.json`.
