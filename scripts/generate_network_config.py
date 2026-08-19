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

_CORE_MAPPING = {
    "stablecoin": ("core", "dUSD"),
    "vault": ("core", "Vault"),
    "factory": ("core", "Factory"),
    "digital_identity": ("core", "DigitalIdentity"),
    "dex_router": ("router", "DclexRouter"),
    "position_manager": ("v3Main", "DclexPositionManager"),
    "oracle": ("router", "FIOracle"),
    "wdel": ("v3Main", "WDEL"),
}

_REQUIRED = ("stablecoin", "vault", "factory", "digital_identity")


def _pick(addresses: dict[str, Any], section: str, key: str) -> str | None:
    return (addresses.get(section) or {}).get(key)


def build_config(addresses: dict[str, Any]) -> dict[str, Any]:
    core: dict[str, str] = {}
    for target, (section, key) in _CORE_MAPPING.items():
        value = _pick(addresses, section, key)
        if value is None:
            if target in _REQUIRED:
                raise ValueError(
                    f"missing required address for {target} ({section}.{key})"
                )
            continue
        core[target] = value
    return {"chain_id": addresses["chainId"], "core": core}


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
