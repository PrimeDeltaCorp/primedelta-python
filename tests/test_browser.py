import http.client
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse

import pytest
from hexbytes import HexBytes

from primedelta import BrowserSigner
from primedelta.browser import (
    BrowserSignerError,
    _render_page,
    _render_session_page,
)

ADDR = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"


def _extract_config(page_html):
    match = re.search(
        r'<script id="config" type="application/json">(.*?)</script>',
        page_html,
        re.S,
    )
    return json.loads(match.group(1))


class _Tab:
    def __init__(self, responder, session=None):
        self.responder = responder
        self.session = session
        self.opened = []
        self.jobs = []
        self.stopped = threading.Event()

    def open(self, url):
        self.opened.append(url)
        threading.Thread(target=self._serve, args=(url,), daemon=True).start()

    def _serve(self, url):
        base = f"http://127.0.0.1:{urlparse(url).port}"
        page = urllib.request.urlopen(url, timeout=5).read().decode()
        session = self.session or _extract_config(page)["session"]
        while not self.stopped.is_set():
            try:
                response = urllib.request.urlopen(
                    f"{base}/next?session={session}", timeout=5
                )
            except Exception:
                return
            if response.status == 204:
                continue
            job = json.loads(response.read())
            self.jobs.append(job)
            value, error = self.responder(job)
            request = urllib.request.Request(
                f"{base}/result?session={session}&state={job['state']}",
                data=json.dumps({"value": value, "error": error}).encode(),
                method="POST",
            )
            try:
                urllib.request.urlopen(request, timeout=5)
            except Exception:
                return


@pytest.fixture
def make_signer():
    created = []

    def factory(responder, timeout=5, session=None, **kwargs):
        signer = BrowserSigner(timeout=timeout, **kwargs)
        signer._bridge.poll_seconds = 0.2
        tab = _Tab(responder, session=session)
        signer._open = tab.open
        created.append((signer, tab))
        return signer, tab

    yield factory
    for signer, tab in created:
        tab.stopped.set()
        signer.close()


def _wallet(job):
    if job["op"] == "connect":
        return ADDR, None
    if job["op"] == "personal_sign":
        return "0xSIGNATURE", None
    return "0x" + "ab" * 32, None


class TestBrowserSigner:
    def test_conforms_to_wallet_signer_shape(self):
        assert BrowserSigner.fills_gas_and_nonce is True
        for member in (
            "address",
            "sign_message",
            "submit_transaction",
            "loopback_origin",
            "close",
        ):
            assert hasattr(BrowserSigner, member)

    def test_address_connects_and_caches(self, make_signer):
        signer, tab = make_signer(_wallet)
        assert signer.address == ADDR
        assert signer.address == ADDR
        assert [job["op"] for job in tab.jobs] == ["connect"]

    def test_address_checksummed_when_wallet_returns_lowercase(self, make_signer):
        signer, _ = make_signer(lambda job: (ADDR.lower(), None))
        assert signer.address == ADDR

    def test_sign_message_uses_personal_sign_with_address(self, make_signer):
        signer, tab = make_signer(_wallet)
        assert signer.sign_message("hello siwe") == "0xSIGNATURE"
        job = tab.jobs[-1]
        assert job["op"] == "personal_sign"
        assert job["params"]["message"] == "hello siwe"
        assert job["params"]["address"] == ADDR

    def test_submit_transaction_sends_and_returns_hash(self, make_signer):
        signer, tab = make_signer(_wallet)
        tx = {"from": ADDR, "to": ADDR, "value": 5, "data": "0xdead", "chainId": 2028}
        result = signer.submit_transaction(None, tx)
        assert isinstance(result, HexBytes)
        assert result == HexBytes("0x" + "ab" * 32)
        assert tab.jobs[-1]["params"]["tx"] == {
            "from": ADDR,
            "to": ADDR,
            "value": hex(5),
            "data": "0xdead",
            "chainId": hex(2028),
        }

    def test_chain_config_forwarded_to_page(self, make_signer):
        chain = {
            "chainId": "0x7ec",
            "chainName": "PrimeDelta Dev",
            "rpcUrls": ["https://besu.dev.primedelta.io"],
            "nativeCurrency": {"name": "DEL", "symbol": "DEL", "decimals": 18},
        }
        signer, tab = make_signer(_wallet, chain=chain)
        assert signer.address == ADDR
        assert tab.jobs[0]["params"]["chain"] == chain

    def test_wallet_error_raises(self, make_signer):
        signer, _ = make_signer(lambda job: (None, "user rejected"))
        with pytest.raises(BrowserSignerError, match="user rejected"):
            _ = signer.address

    def test_one_tab_serves_every_operation(self, make_signer):
        signer, tab = make_signer(_wallet)
        assert signer.address == ADDR
        assert signer.sign_message("siwe") == "0xSIGNATURE"
        signer.submit_transaction(
            None, {"from": ADDR, "to": ADDR, "value": 0, "chainId": 2028}
        )
        assert len(tab.opened) == 1
        assert [job["op"] for job in tab.jobs] == ["connect", "personal_sign", "send"]

    def test_origin_is_stable_and_matches_the_opened_page(self, make_signer):
        signer, tab = make_signer(_wallet)
        origin = signer.loopback_origin
        assert re.fullmatch(r"http://127\.0\.0\.1:\d+", origin)
        assert signer.address == ADDR
        assert tab.opened[0].startswith(origin + "/?session=")
        assert signer.loopback_origin == origin

    def test_a_new_tab_opens_once_the_old_one_stopped_polling(self, make_signer):
        signer, tab = make_signer(_wallet)
        signer._bridge.tab_grace_seconds = 0.05
        assert signer.address == ADDR
        tab.stopped.set()
        time.sleep(0.5)
        tab.stopped.clear()
        assert signer.sign_message("again") == "0xSIGNATURE"
        assert len(tab.opened) == 2

    def test_wrong_session_is_rejected_and_nothing_is_sent(self, make_signer):
        signer, tab = make_signer(_wallet, timeout=1, session="WRONG")
        with pytest.raises(BrowserSignerError, match="nothing was sent"):
            _ = signer.address
        assert tab.jobs == []

    def test_unanswered_request_warns_it_may_still_complete(self, make_signer):
        def slow(job):
            time.sleep(1.5)
            return ADDR, None

        signer, _ = make_signer(slow, timeout=0.5)
        with pytest.raises(BrowserSignerError, match="may still complete"):
            _ = signer.address

    def test_request_from_a_foreign_host_is_refused(self, make_signer):
        signer, _ = make_signer(_wallet)
        port = urlparse(signer.loopback_origin).port
        session = signer._bridge._session
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request(
            "GET", f"/?session={session}", headers={"Host": "evil.example:80"}
        )
        assert connection.getresponse().status == 403
        connection.close()

    def test_result_for_an_unknown_request_is_refused(self, make_signer):
        signer, _ = make_signer(_wallet)
        origin = signer.loopback_origin
        session = signer._bridge._session
        request = urllib.request.Request(
            f"{origin}/result?session={session}&state=nope",
            data=b'{"value": "0x1"}',
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as info:
            urllib.request.urlopen(request, timeout=5)
        assert info.value.code == 404

    def test_port_can_be_pinned_by_env(self, make_signer, monkeypatch):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]
        monkeypatch.setenv("PRIMEDELTA_BROWSER_SIGNER_PORT", str(free_port))
        signer, _ = make_signer(_wallet)
        assert urlparse(signer.loopback_origin).port == free_port

    def test_busy_pinned_port_falls_back_to_a_random_one(self, make_signer, capsys):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            taken = busy.getsockname()[1]
            signer, _ = make_signer(_wallet, port=taken)
            port = urlparse(signer.loopback_origin).port
        assert port != taken
        assert "unavailable" in capsys.readouterr().err

    def test_session_page_is_self_contained(self):
        page = _render_session_page("SESSION123")
        assert _extract_config(page) == {"session": "SESSION123"}
        assert "eip6963:requestProvider" in page
        assert "<script src" not in page
        assert "/next" in page
        assert "eth_sendTransaction" in page

    def test_session_page_escapes_script_breakout(self):
        page = _render_session_page("</script><script>alert(1)</script>")
        assert "<script>alert(1)" not in page
        assert page.count("</script>") == 2

    def test_page_is_self_contained(self):
        page = _render_page("connect", {"chain": None}, "STATE123")
        assert "STATE123" in page
        assert "eip6963:requestProvider" in page
        assert "<script src" not in page
        assert "personal_sign" in page
        assert "eth_sendTransaction" in page

    def test_page_escapes_script_breakout_in_params(self):
        page = _render_page(
            "personal_sign",
            {"message": "</script><script>alert(1)</script>", "address": ADDR},
            "S",
        )
        assert "<script>alert(1)" not in page
        assert page.count("</script>") == 2


def test_opened_url_carries_only_the_session_token(make_signer):
    signer, tab = make_signer(_wallet)
    assert signer.address == ADDR
    query = parse_qs(urlparse(tab.opened[0]).query)
    assert set(query) == {"session"}
