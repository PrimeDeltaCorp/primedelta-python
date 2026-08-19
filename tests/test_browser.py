import json
import re
import threading
import urllib.request
from urllib.parse import parse_qs, urlparse

import pytest
from hexbytes import HexBytes

from primedelta import BrowserSigner
from primedelta.browser import BrowserSignerError, _render_page
from primedelta.signer import Signer

ADDR = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"


def _extract_config(page_html):
    match = re.search(
        r'<script id="config" type="application/json">(.*?)</script>',
        page_html,
        re.S,
    )
    return json.loads(match.group(1))


def _opener(responder, wrong_state=False):
    def open_url(url):
        def worker():
            parts = urlparse(url)
            port = parts.port
            state = parse_qs(parts.query)["state"][0]
            page = urllib.request.urlopen(url, timeout=5).read().decode()
            value, error = responder(_extract_config(page))
            used_state = "WRONG" if wrong_state else state
            body = json.dumps({"value": value, "error": error}).encode()
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/result?state={used_state}",
                data=body,
                method="POST",
            )
            try:
                urllib.request.urlopen(request, timeout=5)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    return open_url


class TestBrowserSigner:
    def _signer(self, responder, timeout=5, **kwargs):
        signer = BrowserSigner(timeout=timeout, **kwargs)
        signer._open = _opener(responder)
        return signer

    def test_is_wallet_signer(self):
        signer = BrowserSigner()
        assert signer.fills_gas_and_nonce is True
        assert isinstance(signer, Signer)

    def test_address_connects_and_caches(self):
        ops = []

        def responder(config):
            ops.append(config["op"])
            return ADDR, None

        signer = self._signer(responder)
        assert signer.address == ADDR
        assert signer.address == ADDR
        assert ops == ["connect"]

    def test_sign_message_uses_personal_sign_with_address(self):
        seen = {}

        def responder(config):
            if config["op"] == "connect":
                return ADDR, None
            seen.update(config)
            return "0xSIGNATURE", None

        signer = self._signer(responder)
        assert signer.sign_message("hello siwe") == "0xSIGNATURE"
        assert seen["op"] == "personal_sign"
        assert seen["params"]["message"] == "hello siwe"
        assert seen["params"]["address"] == ADDR

    def test_submit_transaction_sends_and_returns_hash(self):
        seen = {}

        def responder(config):
            if config["op"] == "connect":
                return ADDR, None
            seen["tx"] = config["params"]["tx"]
            return "0x" + "ab" * 32, None

        signer = self._signer(responder)
        tx = {"from": ADDR, "to": ADDR, "value": 5, "data": "0xdead", "chainId": 2028}
        result = signer.submit_transaction(None, tx)
        assert isinstance(result, HexBytes)
        assert result == HexBytes("0x" + "ab" * 32)
        assert seen["tx"] == {
            "from": ADDR,
            "to": ADDR,
            "value": hex(5),
            "data": "0xdead",
            "chainId": hex(2028),
        }

    def test_chain_config_forwarded_to_page(self):
        chain = {
            "chainId": "0x7ec",
            "chainName": "PrimeDelta Dev",
            "rpcUrls": ["https://besu-dev.primedelta.io"],
            "nativeCurrency": {"name": "DEL", "symbol": "DEL", "decimals": 18},
        }
        seen = {}

        def responder(config):
            seen["chain"] = config["params"]["chain"]
            return ADDR, None

        signer = self._signer(responder, chain=chain)
        assert signer.address == ADDR
        assert seen["chain"] == chain

    def test_wallet_error_raises(self):
        signer = self._signer(lambda config: (None, "user rejected"))
        with pytest.raises(BrowserSignerError):
            _ = signer.address

    def test_wrong_state_is_rejected_and_times_out(self):
        signer = BrowserSigner(timeout=2)
        signer._open = _opener(lambda config: (ADDR, None), wrong_state=True)
        with pytest.raises(BrowserSignerError):
            _ = signer.address

    def test_page_is_self_contained(self):
        page = _render_page("connect", {"chain": None}, "STATE123")
        assert "STATE123" in page
        assert "eip6963:requestProvider" in page
        assert "<script src" not in page
        assert "personal_sign" in page
        assert "eth_sendTransaction" in page
