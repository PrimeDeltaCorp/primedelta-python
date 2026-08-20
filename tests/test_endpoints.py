from unittest.mock import MagicMock, patch

import pytest

from primedelta import PrimeDelta
from primedelta.settings import resolve_endpoints


class TestResolveEndpoints:
    def _clear(self, monkeypatch):
        for var in (
            "PRIMEDELTA_BASE_URL",
            "PRIMEDELTA_APP_URL",
            "PRIMEDELTA_SIWE_DOMAIN",
        ):
            monkeypatch.delenv(var, raising=False)

    def test_dev_defaults(self, monkeypatch):
        self._clear(monkeypatch)
        ep = resolve_endpoints("dev")
        assert ep.base_url == "https://api-dev.primedelta.io"
        assert ep.app_url == "https://mint-dev.primedelta.io"
        assert ep.siwe_domain == "mint-dev.primedelta.io"
        assert ep.siwe_uri == "https://mint-dev.primedelta.io"

    def test_testnet_defaults(self, monkeypatch):
        self._clear(monkeypatch)
        ep = resolve_endpoints("testnet")
        assert ep.base_url == "https://api-testnet.primedelta.io"
        assert ep.siwe_domain == "mint-testnet.primedelta.io"

    def test_mainnet_defaults(self, monkeypatch):
        self._clear(monkeypatch)
        ep = resolve_endpoints("mainnet")
        assert ep.base_url == "https://api-mainnet.primedelta.io"
        assert ep.siwe_domain == "mint-mainnet.primedelta.io"

    def test_env_overrides_win(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("PRIMEDELTA_BASE_URL", "http://localhost:8000")
        monkeypatch.setenv("PRIMEDELTA_APP_URL", "http://localhost:5173")
        ep = resolve_endpoints("testnet")
        assert ep.base_url == "http://localhost:8000"
        assert ep.app_url == "http://localhost:5173"
        assert ep.siwe_domain == "localhost:5173"

    def test_siwe_domain_override(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("PRIMEDELTA_SIWE_DOMAIN", "localhost")
        ep = resolve_endpoints("dev")
        assert ep.siwe_domain == "localhost"

    def test_unknown_network_without_env_raises(self, monkeypatch):
        self._clear(monkeypatch)
        with pytest.raises(ValueError, match="no default endpoints"):
            resolve_endpoints("staging")

    def test_unknown_network_with_env_resolves(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("PRIMEDELTA_BASE_URL", "https://api-x.example.io")
        monkeypatch.setenv("PRIMEDELTA_APP_URL", "https://app-x.example.io")
        ep = resolve_endpoints("staging")
        assert ep.base_url == "https://api-x.example.io"


class TestLoginDomainPerNetwork:
    def _pd(self, network, monkeypatch):
        for var in (
            "PRIMEDELTA_BASE_URL",
            "PRIMEDELTA_APP_URL",
            "PRIMEDELTA_SIWE_DOMAIN",
        ):
            monkeypatch.delenv(var, raising=False)
        with patch("primedelta.primedelta.Web3"):
            pd = PrimeDelta(
                private_key="0x" + "1" * 64,
                web3_provider_url="http://x",
                network=network,
            )
        pd._primedelta_client = MagicMock()
        pd._primedelta_client.get_nonce.return_value = "nonce123"
        pd._signer = MagicMock()
        pd._signer.address = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
        captured = {}
        pd._signer.sign_message.side_effect = lambda msg: captured.setdefault(
            "msg", msg
        )
        return pd, captured

    def test_dev_login_signs_dev_domain(self, monkeypatch):
        pd, captured = self._pd("dev", monkeypatch)
        pd.login()
        # A SIWE message begins with "<domain> wants you to sign in ...", so the
        # first token IS the domain. Compare it by equality (not `in`/startswith,
        # which pin the URI line too and trip CodeQL's url-substring rule).
        assert captured["msg"].split()[0] == "mint-dev.primedelta.io"

    def test_testnet_login_signs_testnet_domain(self, monkeypatch):
        pd, captured = self._pd("testnet", monkeypatch)
        pd.login()
        assert captured["msg"].split()[0] == "mint-testnet.primedelta.io"
        # chain id in the SIWE message follows the network config (7357).
        assert "Chain ID: 7357" in captured["msg"]

    def test_client_base_url_follows_network(self, monkeypatch):
        for var in ("PRIMEDELTA_BASE_URL", "PRIMEDELTA_APP_URL"):
            monkeypatch.delenv(var, raising=False)
        with patch("primedelta.primedelta.Web3"):
            pd = PrimeDelta(
                private_key="0x" + "1" * 64,
                web3_provider_url="http://x",
                network="testnet",
            )
        assert pd._primedelta_client._base_url == "https://api-testnet.primedelta.io"
