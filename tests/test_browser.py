import http.client
import json
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse

import pytest
from hexbytes import HexBytes

from primedelta import BrowserSigner
from primedelta.browser import (
    _WALLET_HELPERS,
    BrowserSignerError,
    _loopback_port,
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


class TestBridgeEdges:
    def _post(self, signer, state, body=b'{"value": "0x1"}'):
        request = urllib.request.Request(
            f"{signer.loopback_origin}/result?session={signer._bridge._session}"
            f"&state={state}",
            data=body,
            method="POST",
        )
        try:
            return urllib.request.urlopen(request, timeout=5).status
        except urllib.error.HTTPError as exc:
            return exc.code

    def _next(self, signer, timeout=5):
        url = f"{signer.loopback_origin}/next?session={signer._bridge._session}"
        return urllib.request.urlopen(url, timeout=timeout)

    def test_a_slow_wallet_approval_does_not_open_a_second_tab(self, make_signer):
        def slow(job):
            if job["op"] == "connect":
                time.sleep(0.6)
            return _wallet(job)

        signer, tab = make_signer(slow)
        signer._bridge.tab_grace_seconds = 0.05
        assert signer.address == ADDR
        assert signer.sign_message("siwe") == "0xSIGNATURE"
        assert len(tab.opened) == 1

    def test_an_abandoned_poll_does_not_swallow_the_next_request(self, make_signer):
        signer, tab = make_signer(_wallet)
        bridge = signer._bridge
        bridge.liveness_seconds = 0.05
        bridge.poll_seconds = 5
        port = urlparse(signer.loopback_origin).port
        orphan = socket.create_connection(("127.0.0.1", port), timeout=5)
        orphan.sendall(
            f"GET /next?session={bridge._session} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n\r\n".encode()
        )
        deadline = time.monotonic() + 2
        while bridge._polling == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        orphan.close()
        assert signer.address == ADDR
        assert [job["op"] for job in tab.jobs] == ["connect"]
        assert len(tab.opened) == 1

    def test_a_request_with_no_tab_reopens_one(self, make_signer):
        signer, tab = make_signer(_wallet)
        signer._bridge.reopen_seconds = 0.2
        signer._bridge.liveness_seconds = 0.05
        opened = []
        signer._open = opened.append
        threading.Timer(0.5, lambda: tab.open(opened[-1])).start()
        assert signer.address == ADDR
        assert len(opened) >= 2

    def test_close_fails_a_pending_request_at_once(self, make_signer):
        signer, _ = make_signer(_wallet, timeout=30)
        signer._open = lambda url: None
        errors = []

        def ask():
            try:
                _ = signer.address
            except BrowserSignerError as exc:
                errors.append(str(exc))

        worker = threading.Thread(target=ask)
        started = time.monotonic()
        worker.start()
        while not signer._bridge._jobs and time.monotonic() - started < 2:
            time.sleep(0.01)
        signer.close()
        worker.join(timeout=5)
        assert time.monotonic() - started < 5
        assert errors and "nothing was sent" in errors[0]
        with pytest.raises(BrowserSignerError, match="closed"):
            signer.sign_message("after close")

    def test_close_releases_a_pending_long_poll(self, make_signer):
        signer, _ = make_signer(_wallet)
        signer._bridge.poll_seconds = 30
        signer._bridge.liveness_seconds = 30
        origin = signer.loopback_origin
        statuses = []

        def poll():
            try:
                statuses.append(self._next(signer, timeout=10).status)
            except Exception as exc:
                statuses.append(type(exc).__name__)

        worker = threading.Thread(target=poll)
        worker.start()
        while signer._bridge._polling == 0:
            time.sleep(0.01)
        started = time.monotonic()
        signer.close()
        worker.join(timeout=10)
        assert time.monotonic() - started < 5
        assert statuses and origin

    def test_a_result_for_an_undispatched_request_is_refused(self, make_signer):
        signer, _ = make_signer(_wallet, timeout=1)
        signer._open = lambda url: None
        codes = []

        def answer_early():
            while not signer._bridge._jobs:
                time.sleep(0.01)
            state = next(iter(signer._bridge._jobs))
            codes.append(self._post(signer, state, b'{"value": "0xforged"}'))

        threading.Thread(target=answer_early).start()
        with pytest.raises(BrowserSignerError, match="nothing was sent"):
            _ = signer.address
        assert codes == [404]

    def test_a_second_result_for_the_same_request_is_refused(self, make_signer):
        signer, _ = make_signer(_wallet)
        signer._open = lambda url: None
        codes = []

        def tab():
            job = json.loads(self._next(signer).read())
            codes.append(
                self._post(signer, job["state"], b'{"value": "' + ADDR.encode() + b'"}')
            )
            codes.append(self._post(signer, job["state"], b'{"value": "0xother"}'))

        worker = threading.Thread(target=tab)
        worker.start()
        assert signer.address == ADDR
        worker.join(timeout=5)
        assert codes == [204, 404]

    def test_a_timed_out_request_leaves_the_queue_clean(self, make_signer):
        signer, _ = make_signer(_wallet, timeout=0.3)
        signer._open = lambda url: None
        with pytest.raises(BrowserSignerError, match="nothing was sent"):
            _ = signer.address
        assert signer._bridge._queue == []
        assert self._next(signer).status == 204

    @pytest.mark.parametrize("raw", ["abc", "70000", "-1", ""])
    def test_a_bad_port_env_falls_back_to_a_random_port(self, monkeypatch, raw):
        monkeypatch.setenv("PRIMEDELTA_BROWSER_SIGNER_PORT", raw)
        assert _loopback_port(None) == 0

    def test_an_out_of_range_port_argument_falls_back(self, make_signer, capsys):
        signer, _ = make_signer(_wallet, port=70000)
        assert 0 < urlparse(signer.loopback_origin).port <= 65535
        assert "unavailable" in capsys.readouterr().err

    def test_both_pages_refuse_a_different_wallet_account(self):
        for page in (
            _render_session_page("S"),
            _render_page("send", {"tx": {"from": ADDR}}, "S"),
        ):
            assert "Switch your wallet to" in page
            assert "accounts.some" in page


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
class TestWalletJs:
    def _run(self, op, params, accounts):
        script = (
            "globalThis.document={getElementById:()=>({textContent:''})};"
            + _WALLET_HELPERS
            + "const provider={request: async ({method}) => {"
            + f"if (method === 'eth_requestAccounts') return {json.dumps(accounts)};"
            + "if (method === 'wallet_switchEthereumChain') return null;"
            + "return 'SIGNED:' + method; }};"
            + f"perform(provider, {json.dumps(op)}, {json.dumps(params)})"
            + ".then(v => console.log('OK ' + v), e => console.log('ERR ' + e.message));"
        )
        result = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, timeout=30
        )
        return result.stdout.strip()

    def test_send_from_another_account_is_refused(self):
        out = self._run("send", {"tx": {"from": ADDR}}, ["0x" + "1" * 40])
        assert out.startswith("ERR Switch your wallet to")

    def test_sign_with_another_account_is_refused(self):
        out = self._run(
            "personal_sign", {"message": "m", "address": ADDR}, ["0x" + "1" * 40]
        )
        assert out.startswith("ERR Switch your wallet to")

    def test_the_connected_account_signs_whatever_its_case(self):
        out = self._run(
            "personal_sign", {"message": "m", "address": ADDR}, [ADDR.lower()]
        )
        assert out == "OK SIGNED:personal_sign"


def test_opened_url_carries_only_the_session_token(make_signer):
    signer, tab = make_signer(_wallet)
    assert signer.address == ADDR
    query = parse_qs(urlparse(tab.opened[0]).query)
    assert set(query) == {"session"}
