import importlib.util
from pathlib import Path

import pytest

from primedelta.networks import load

_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "generate_network_config.py"
)
_spec = importlib.util.spec_from_file_location("gen_network_config", _SCRIPT)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


_DEV_SHAPE = {
    "chainId": 2028,
    "core": {"dUSD": "0xdUSD", "Vault": "0xVault", "Factory": "0xFac", "DigitalIdentity": "0xDID"},
    "router": {"DclexRouter": "0xRouter", "FIOracle": "0xOracle"},
    "v3Main": {"WDEL": "0xWDEL", "Quoter": "0xQuoter", "DclexPositionManager": "0xPM"},
}

# testnet/mainnet: chain_id, router_stack + v3; v3 also carries an UNUSED phase-3 PM.
_TESTNET_SHAPE = {
    "chain_id": 7357,
    "core": {"dUSD": "0xdUSD", "Vault": "0xVault", "Factory": "0xFac", "DigitalIdentity": "0xDID"},
    "router_stack": {
        "DclexRouter": "0xRouter",
        "FIOracle": "0xOracle",
        "DclexPositionManager": "0xCanonicalPM",
    },
    "v3": {
        "WDEL": "0xWDEL",
        "Quoter": "0xQuoter",
        "DclexPositionManager_phase3_unused": "0xStalePM",
    },
}


class TestBuildConfig:
    def test_dev_shape_maps_all_targets(self):
        cfg = gen.build_config(_DEV_SHAPE)
        assert cfg["chain_id"] == 2028
        assert cfg["core"]["stablecoin"] == "0xdUSD"
        assert cfg["core"]["dex_router"] == "0xRouter"
        assert cfg["core"]["oracle"] == "0xOracle"
        assert cfg["core"]["position_manager"] == "0xPM"
        assert cfg["core"]["quoter"] == "0xQuoter"

    def test_testnet_shape_uses_router_stack_and_v3(self):
        cfg = gen.build_config(_TESTNET_SHAPE)
        assert cfg["chain_id"] == 7357
        assert cfg["core"]["dex_router"] == "0xRouter"
        assert cfg["core"]["oracle"] == "0xOracle"
        assert cfg["core"]["wdel"] == "0xWDEL"
        assert cfg["core"]["quoter"] == "0xQuoter"

    def test_position_manager_candidate_order_prefers_router_stack(self):
        # Both candidate LOCATIONS present (router_stack.DclexPositionManager and
        # the fallback v3Main.DclexPositionManager) so the candidate ORDER — not
        # a missing key — decides. Reversing the candidate list flips this.
        shape = {
            "chain_id": 7357,
            "core": _TESTNET_SHAPE["core"],
            "router_stack": {
                "DclexRouter": "0xRouter",
                "FIOracle": "0xOracle",
                "DclexPositionManager": "0xCanonical",
            },
            "v3Main": {"DclexPositionManager": "0xStale"},
        }
        cfg = gen.build_config(shape)
        assert cfg["core"]["position_manager"] == "0xCanonical"

    def test_missing_required_raises(self):
        broken = {"chain_id": 1, "core": {"dUSD": "0x", "Vault": "0x", "Factory": "0x"}}
        with pytest.raises(ValueError, match="digital_identity"):
            gen.build_config(broken)

    def test_missing_chain_id_raises(self):
        with pytest.raises(ValueError, match="chainId nor chain_id"):
            gen.build_config({"core": _DEV_SHAPE["core"]})


class TestLoadTestnet:
    def test_testnet_loads_with_full_core(self):
        contracts = load("testnet")
        assert contracts.chain_id == 7357
        core = contracts.core
        assert core.quoter is not None
        assert core.position_manager is not None
        assert core.wdel is not None
        assert core.oracle is not None
