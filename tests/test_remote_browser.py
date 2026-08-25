import json
import re
import threading
from urllib.parse import parse_qs, urlparse

import pytest
from hexbytes import HexBytes

from primedelta import RemoteBrowserSigner
from primedelta.browser import BrowserSignerError

ADDR = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
BASE = "https://signer.example"


def _extract_config(page_html):
    match = re.search(
        r'<script id="config" type="application/json">(.*?)</script>', page_html, re.S
    )
    return json.loads(match.group(1))


def _signer(responder, timeout=5, wrong_state=False, capture=None, **kwargs):
    """A RemoteBrowserSigner whose `deliver` simulates the hosting app: it renders
    the pending page, runs `responder` on the config, and posts the result back
    through the signer's own resolve() hook (no HTTP needed)."""
    holder = {}

    def deliver(url):
        if capture is not None:
            capture.append(url)
        signer = holder["signer"]

        def worker():
            state = parse_qs(urlparse(url).query)["state"][0]
            page = signer.render_page(state)
            value, error = responder(_extract_config(page))
            signer.resolve("WRONG" if wrong_state else state, value=value, error=error)

        threading.Thread(target=worker, daemon=True).start()

    signer = RemoteBrowserSigner(
        base_url=BASE, deliver=deliver, timeout=timeout, **kwargs
    )
    holder["signer"] = signer
    return signer


class TestRemoteBrowserSigner:
    def test_conforms_to_wallet_signer_shape(self):
        assert RemoteBrowserSigner.fills_gas_and_nonce is True
        for member in ("address", "sign_message", "submit_transaction"):
            assert hasattr(RemoteBrowserSigner, member)
        # hosting-app hooks
        for hook in ("render_page", "resolve"):
            assert hasattr(RemoteBrowserSigner, hook)

    def test_address_connects_and_caches(self):
        ops = []

        def responder(config):
            ops.append(config["op"])
            return ADDR, None

        signer = _signer(responder)
        assert signer.address == ADDR
        assert signer.address == ADDR
        assert ops == ["connect"]

    def test_sign_message_uses_personal_sign(self):
        seen = {}

        def responder(config):
            if config["op"] == "connect":
                return ADDR, None
            seen.update(config)
            return "0xSIG", None

        assert _signer(responder).sign_message("hello siwe") == "0xSIG"
        assert seen["op"] == "personal_sign"
        assert seen["params"]["message"] == "hello siwe"
        assert seen["params"]["address"] == ADDR

    def test_submit_transaction_shape_and_hash(self):
        seen = {}

        def responder(config):
            if config["op"] == "connect":
                return ADDR, None
            seen["tx"] = config["params"]["tx"]
            return "0x" + "ab" * 32, None

        tx = {"from": ADDR, "to": ADDR, "value": 5, "data": "0xdead", "chainId": 2028}
        result = _signer(responder).submit_transaction(None, tx)
        assert result == HexBytes("0x" + "ab" * 32)
        assert seen["tx"] == {
            "from": ADDR,
            "to": ADDR,
            "value": hex(5),
            "data": "0xdead",
            "chainId": hex(2028),
        }

    def test_chain_forwarded_to_page(self):
        chain = {"chainId": "0x7ec", "chainName": "PrimeDelta dev"}
        seen = {}

        def responder(config):
            seen["chain"] = config["params"]["chain"]
            return ADDR, None

        assert _signer(responder, chain=chain).address == ADDR
        assert seen["chain"] == chain

    def test_delivered_url_is_hosted_and_carries_only_the_token(self):
        urls = []

        def responder(config):
            if config["op"] == "connect":
                return ADDR, None
            return "0xSIG", None

        # exercise a signing op too (not just connect) so a regression that
        # appended the message to the URL would fail here.
        secret = "super-secret-siwe-body"
        _signer(responder, capture=urls).sign_message(secret)
        assert len(urls) == 2  # connect + personal_sign
        for url in urls:
            parsed = urlparse(url)
            assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == f"{BASE}/sign"
            # the query is exactly the opaque state token — no tx/message params.
            # (substring-checking the raw URL is wrong: the random token can itself
            # contain "tx".)
            assert set(parse_qs(parsed.query)) == {"state"}
            # and the signed payload never leaks into the URL
            assert secret not in url

    def test_requires_https_or_localhost_base_url(self):
        with pytest.raises(ValueError):
            RemoteBrowserSigner(
                base_url="http://signer.example", deliver=lambda u: None
            )
        # localhost over http is allowed for local testing
        RemoteBrowserSigner(base_url="http://127.0.0.1:8080", deliver=lambda u: None)

    def test_wallet_error_raises(self):
        with pytest.raises(BrowserSignerError):
            _ = _signer(lambda c: (None, "user rejected")).address

    def test_unknown_state_resolve_returns_false_and_times_out(self):
        signer = _signer(lambda c: (ADDR, None), timeout=1, wrong_state=True)
        with pytest.raises(BrowserSignerError):
            _ = signer.address

    def test_render_page_unknown_state_raises(self):
        signer = RemoteBrowserSigner(base_url=BASE, deliver=lambda url: None)
        with pytest.raises(BrowserSignerError):
            signer.render_page("nope")

    def test_resolve_unknown_state_is_false(self):
        signer = RemoteBrowserSigner(base_url=BASE, deliver=lambda url: None)
        assert signer.resolve("nope", value="x") is False
