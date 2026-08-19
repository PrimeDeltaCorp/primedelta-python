import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from hexbytes import HexBytes


class BrowserSignerError(Exception):
    pass


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>PrimeDelta wallet</title></head>
<body style="font-family:system-ui;max-width:32rem;margin:4rem auto;text-align:center">
<h2>PrimeDelta</h2>
<p id="status">Connecting to your wallet…</p>
<script id="config" type="application/json">%(config)s</script>
<script>
const CONFIG = JSON.parse(document.getElementById("config").textContent);
const setStatus = (t) => { document.getElementById("status").textContent = t; };

function discoverProvider() {
  return new Promise((resolve) => {
    let picked = null;
    const onAnnounce = (e) => { picked = picked || e.detail.provider; };
    window.addEventListener("eip6963:announceProvider", onAnnounce);
    window.dispatchEvent(new Event("eip6963:requestProvider"));
    setTimeout(() => {
      window.removeEventListener("eip6963:announceProvider", onAnnounce);
      resolve(picked || window.ethereum || null);
    }, 300);
  });
}

function utf8ToHex(str) {
  const bytes = new TextEncoder().encode(str);
  let out = "0x";
  for (const b of bytes) out += b.toString(16).padStart(2, "0");
  return out;
}

async function maybeSwitchChain(provider) {
  const chain = CONFIG.params.chain;
  if (!chain) return;
  try {
    await provider.request({method: "wallet_switchEthereumChain", params: [{chainId: chain.chainId}]});
  } catch (e) {
    if (e && e.code === 4902) {
      await provider.request({method: "wallet_addEthereumChain", params: [chain]});
    } else { throw e; }
  }
}

async function run() {
  const provider = await discoverProvider();
  if (!provider) throw new Error("No EIP-1193 wallet found");
  const accounts = await provider.request({method: "eth_requestAccounts"});
  await maybeSwitchChain(provider);
  if (CONFIG.op === "connect") return accounts[0];
  if (CONFIG.op === "personal_sign") {
    return await provider.request({method: "personal_sign", params: [utf8ToHex(CONFIG.params.message), CONFIG.params.address]});
  }
  if (CONFIG.op === "send") {
    return await provider.request({method: "eth_sendTransaction", params: [CONFIG.params.tx]});
  }
  throw new Error("Unknown operation");
}

function report(value, error) {
  return fetch("/result?state=" + encodeURIComponent(CONFIG.state), {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({value: value, error: error}),
  });
}

run().then(
  (value) => { setStatus("Done — you can close this tab."); return report(value, null); },
  (err) => { const m = (err && err.message) || String(err); setStatus("Failed: " + m); return report(null, m); }
);
</script>
</body></html>
"""


def _render_page(op: str, params: dict, state: str) -> str:
    config = json.dumps({"op": op, "params": params, "state": state})
    config = (
        config.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    )
    return _PAGE % {"config": config}


class _LoopbackBridge:
    def __init__(self, timeout: float) -> None:
        self._timeout = timeout

    def request(self, html: str, state: str, opener: Callable[[str], None]) -> Any:
        result_box: dict = {}
        done = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                if urlparse(self.path).path != "/":
                    self.send_response(404)
                    self.end_headers()
                    return
                body = html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                if parsed.path != "/result" or query.get("state", [None])[0] != state:
                    self.send_response(403)
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length", 0))
                result_box["payload"] = json.loads(self.rfile.read(length) or b"{}")
                self.send_response(204)
                self.end_headers()
                done.set()

        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            opener(f"http://127.0.0.1:{port}/?state={state}")
            if not done.wait(self._timeout):
                raise BrowserSignerError("timed out waiting for the wallet")
            payload = result_box["payload"]
            if payload.get("error"):
                raise BrowserSignerError(payload["error"])
            return payload.get("value")
        finally:
            server.shutdown()
            server.server_close()


class BrowserSigner:
    """Sign through a browser wallet (MetaMask, …) via a one-shot local bridge.

    Each operation opens a single-use page on 127.0.0.1 that discovers the wallet
    (EIP-6963), performs `eth_requestAccounts` / `personal_sign` /
    `eth_sendTransaction`, and posts the result back over a one-time state token;
    the loopback server then shuts down. Pass `chain` (a `wallet_addEthereumChain`
    params dict) to switch/add the network before signing.
    """

    fills_gas_and_nonce = True

    def __init__(self, *, chain: Optional[dict] = None, timeout: float = 180.0) -> None:
        self._chain = chain
        self._bridge = _LoopbackBridge(timeout)
        self._address: Optional[str] = None

    def _open(self, url: str) -> None:
        webbrowser.open(url)

    def _run(self, op: str, params: dict) -> Any:
        params = {**params, "chain": self._chain}
        state = secrets.token_urlsafe(32)
        html = _render_page(op, params, state)
        return self._bridge.request(html, state, self._open)

    @property
    def address(self) -> str:
        if self._address is None:
            self._address = self._run("connect", {})
        return self._address

    def sign_message(self, message: str) -> str:
        return self._run("personal_sign", {"message": message, "address": self.address})

    def submit_transaction(self, web3: Any, transaction: dict) -> Any:
        tx = {
            "from": transaction["from"],
            "to": transaction["to"],
            "value": hex(transaction.get("value", 0)),
            "data": transaction.get("data") or "0x",
            "chainId": hex(transaction["chainId"]),
        }
        return HexBytes(self._run("send", {"tx": tx}))
