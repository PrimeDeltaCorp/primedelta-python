"""Generate a bundled `networks/<network>.json` from a deployment addresses file.

The blockchain repo records each redeploy in
`blockchain/deployments/<env>/addresses.json`. This script maps that file onto
the snake_case schema the SDK's `networks.load()` expects, so the bundled config
can be refreshed after every chain redeploy without hand-editing addresses.

Usage:
    python scripts/generate_network_config.py \
        --addresses /path/to/blockchain/deployments/primedelta-dev/addresses.json \
        --network dev
"""

import argparse
import json
from pathlib import Path
from typing import Any

# Deployment `addresses.json` files come in two shapes:
#   dev:            chainId, sections core / router / v3Main
#   testnet+mainnet: chain_id, sections core / router_stack / v3
#     (v3.DclexPositionManager there is the unused phase-3 one; the canonical
#      PM lives in router_stack, so that candidate is listed first).
# Each target lists candidate (section, key) locations tried in order.
_CORE_MAPPING = {
    "stablecoin": [("core", "dUSD")],
    "vault": [("core", "Vault")],
    "factory": [("core", "Factory")],
    "digital_identity": [("core", "DigitalIdentity")],
    "dex_router": [("router", "DclexRouter"), ("router_stack", "DclexRouter")],
    "position_manager": [
        ("router_stack", "DclexPositionManager"),
        ("v3Main", "DclexPositionManager"),
    ],
    "oracle": [("router", "FIOracle"), ("router_stack", "FIOracle")],
    "wdel": [("v3Main", "WDEL"), ("v3", "WDEL")],
    "quoter": [("v3Main", "Quoter"), ("v3", "Quoter")],
}

_REQUIRED = ("stablecoin", "vault", "factory", "digital_identity")


def _pick(addresses: dict[str, Any], candidates: list[tuple[str, str]]) -> str | None:
    for section, key in candidates:
        value = (addresses.get(section) or {}).get(key)
        if value:
            return value
    return None


def build_config(addresses: dict[str, Any]) -> dict[str, Any]:
    core: dict[str, str] = {}
    for target, candidates in _CORE_MAPPING.items():
        value = _pick(addresses, candidates)
        if value is None:
            if target in _REQUIRED:
                locations = " / ".join(f"{s}.{k}" for s, k in candidates)
                raise ValueError(f"missing required address for {target} ({locations})")
            continue
        core[target] = value
    chain_id = addresses.get("chainId", addresses.get("chain_id"))
    if chain_id is None:
        raise ValueError("addresses file has neither chainId nor chain_id")
    return {"chain_id": chain_id, "core": core}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--addresses", required=True, type=Path)
    parser.add_argument("--network", required=True)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output path (default: src/primedelta/networks/<network>.json)",
    )
    args = parser.parse_args()

    addresses = json.loads(args.addresses.read_text())
    config = build_config(addresses)

    out = args.out or (
        Path(__file__).resolve().parent.parent
        / "src"
        / "primedelta"
        / "networks"
        / f"{args.network}.json"
    )
    out.write_text(json.dumps(config, indent=2) + "\n")
    print(f"wrote {out}")
    print(json.dumps(config, indent=2))


if __name__ == "__main__":
    main()
