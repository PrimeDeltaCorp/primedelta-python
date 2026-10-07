from unittest.mock import MagicMock, patch

import pytest

from primedelta import BrowserSigner, PrimeDelta
from primedelta.settings import resolve_endpoints


class TestResolveEndpoints:
    def _clear(self, monkeypatch):
        for var in (
            "PRIMEDELTA_BASE_URL",
            "PRIMEDELTA_APP_URL",
            "PRIMEDELTA_SIWE_DOMAIN",
            "PRIMEDELTA_SIWE_LOOPBACK",
        ):
            monkeypatch.delenv(var, raising=False)

    def test_loopback_sign_in_is_on_for_dev_only(self, monkeypatch):
        self._clear(monkeypatch)
        assert resolve_endpoints("dev").siwe_loopback is True
        assert resolve_endpoints("testnet").siwe_loopback is False
        assert resolve_endpoints("mainnet").siwe_loopback is False

    def test_loopback_sign_in_is_off_when_endpoints_are_overridden(self, monkeypatch):
        for var, value in (
            ("PRIMEDELTA_BASE_URL", "http://localhost:8000"),
            ("PRIMEDELTA_APP_URL", "http://localhost:5173"),
            ("PRIMEDELTA_SIWE_DOMAIN", "localhost"),
        ):
            self._clear(monkeypatch)
            monkeypatch.setenv(var, value)
            assert resolve_endpoints("dev").siwe_loopback is False

    def test_loopback_sign_in_env_wins(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("PRIMEDELTA_SIWE_LOOPBACK", "1")
        assert resolve_endpoints("testnet").siwe_loopback is True
        monkeypatch.setenv("PRIMEDELTA_SIWE_LOOPBACK", "0")
        assert resolve_endpoints("dev").siwe_loopback is False

    def test_dev_defaults(self, monkeypatch):
        self._clear(monkeypatch)
        ep = resolve_endpoints("dev")
        assert ep.base_url == "https://api.dev.primedelta.io"
        assert ep.app_url == "https://mint.dev.primedelta.io"
        assert ep.siwe_domain == "mint.dev.primedelta.io"
        assert ep.siwe_uri == "https://mint.dev.primedelta.io"

    def test_testnet_defaults(self, monkeypatch):
        self._clear(monkeypatch)
        ep = resolve_endpoints("testnet")
        assert ep.base_url == "https://api.testnet.primedelta.io"
        assert ep.siwe_domain == "mint.testnet.primedelta.io"

    def test_mainnet_defaults(self, monkeypatch):
        self._clear(monkeypatch)
        ep = resolve_endpoints("mainnet")
        assert ep.base_url == "https://api.primedelta.io"
        assert ep.siwe_domain == "mint.primedelta.io"

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
            "PRIMEDELTA_SIWE_LOOPBACK",
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
        assert captured["msg"].split()[0] == "mint.dev.primedelta.io"

    def test_testnet_login_signs_testnet_domain(self, monkeypatch):
        pd, captured = self._pd("testnet", monkeypatch)
        pd.login()
        assert captured["msg"].split()[0] == "mint.testnet.primedelta.io"
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
        assert pd._primedelta_client._base_url == "https://api.testnet.primedelta.io"


class TestLoopbackSignIn:
    ADDR = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"

    def _login(self, network, monkeypatch):
        for var in (
            "PRIMEDELTA_BASE_URL",
            "PRIMEDELTA_APP_URL",
            "PRIMEDELTA_SIWE_DOMAIN",
            "PRIMEDELTA_SIWE_LOOPBACK",
        ):
            monkeypatch.delenv(var, raising=False)
        signer = BrowserSigner(port=0)
        signed = []

        def run(op, params):
            if op == "connect":
                return self.ADDR
            signed.append(params["message"])
            return "0x" + "11" * 65

        signer._run = run
        with patch("primedelta.primedelta.Web3"):
            pd = PrimeDelta(
                signer=signer, web3_provider_url="http://x", network=network
            )
        pd._primedelta_client = MagicMock()
        pd._primedelta_client.get_nonce.return_value = "nonce12345"
        try:
            pd.login()
            origin = signer.loopback_origin
        finally:
            signer.close()
        return signed[0], origin

    def test_browser_signer_signs_in_on_its_loopback_origin_on_dev(self, monkeypatch):
        message, origin = self._login("dev", monkeypatch)
        assert message.split()[0] == "127.0.0.1"
        assert f"URI: {origin}" in message.splitlines()

    def test_browser_signer_keeps_the_app_domain_elsewhere(self, monkeypatch):
        message, _ = self._login("testnet", monkeypatch)
        assert message.split()[0] == "mint.testnet.primedelta.io"
