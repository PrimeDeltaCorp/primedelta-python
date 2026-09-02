import json
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from eth_utils import to_checksum_address
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


def _render_page(op: str, params: dict[str, Any], state: str) -> str:
    config = json.dumps({"op": op, "params": params, "state": state})
    config = (
        config.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    )
    return _PAGE % {"config": config}


class _LoopbackBridge:
    def __init__(self, timeout: float) -> None:
        self._timeout = timeout

    def request(self, html: str, state: str, opener: Callable[[str], None]) -> Any:
        result_box: dict[str, Any] = {}
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

    def __init__(
        self, *, chain: Optional[dict[str, Any]] = None, timeout: float = 180.0
    ) -> None:
        self._chain = chain
        self._bridge = _LoopbackBridge(timeout)
        self._address: Optional[str] = None

    def _open(self, url: str) -> None:
        # Always surface the URL so a user whose default browser has no wallet
        # (e.g. Safari without MetaMask) can paste it into the right one, and so
        # a retry after an error is copy-pasteable. Then best-effort auto-open.
        print(
            "\nPrimeDelta wallet: approve in a browser signed into your wallet "
            "(MetaMask / Rabby / …). If the wrong browser opened or it has no "
            f"wallet, paste this URL into the right one:\n  {url}\n",
            file=sys.stderr,
            flush=True,
        )
        try:
            webbrowser.open(url)
        except Exception:
            pass

    def _run(self, op: str, params: dict[str, Any]) -> Any:
        params = {**params, "chain": self._chain}
        state = secrets.token_urlsafe(32)
        html = _render_page(op, params, state)
        return self._bridge.request(html, state, self._open)

    @property
    def address(self) -> str:
        if self._address is None:
            # Wallets return the address lowercased; SIWE requires EIP-55.
            self._address = to_checksum_address(self._run("connect", {}))
        return self._address

    def sign_message(self, message: str) -> str:
        return self._run("personal_sign", {"message": message, "address": self.address})

    def submit_transaction(self, web3: Any, transaction: dict[str, Any]) -> Any:
        tx = {
            "from": transaction["from"],
            "to": transaction["to"],
            "value": hex(transaction.get("value", 0)),
            "data": transaction.get("data") or "0x",
            "chainId": hex(transaction["chainId"]),
        }
        return HexBytes(self._run("send", {"tx": tx}))


class _RemoteBridge:
    """Like `_LoopbackBridge` but for a HOSTED origin: the wallet page is served
    by the hosting app (not a per-op 127.0.0.1 server). A pending operation is
    parked under a one-time state token; the hosting app renders it (GET /sign)
    and delivers the result (POST /result -> `resolve`). Thread-safe; supports
    concurrent users, each on their own token."""

    def __init__(
        self, base_url: str, deliver: Callable[[str], None], timeout: float
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._deliver = deliver
        self._timeout = timeout
        self._pending: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def request(self, op: str, params: dict[str, Any]) -> Any:
        state = secrets.token_urlsafe(32)
        event = threading.Event()
        with self._lock:
            self._pending[state] = {"op": op, "params": params, "event": event}
        try:
            self._deliver(f"{self._base_url}/sign?state={state}")
            if not event.wait(self._timeout):
                raise BrowserSignerError("timed out waiting for the wallet")
            with self._lock:
                payload = self._pending[state].get("result") or {}
        finally:
            with self._lock:
                self._pending.pop(state, None)
        if payload.get("error"):
            raise BrowserSignerError(payload["error"])
        return payload.get("value")

    def render(self, state: str) -> str:
        with self._lock:
            entry = self._pending.get(state)
        if entry is None:
            raise BrowserSignerError("unknown or expired state")
        return _render_page(entry["op"], entry["params"], state)

    def resolve(
        self, state: str, value: Any = None, error: Optional[str] = None
    ) -> bool:
        with self._lock:
            entry = self._pending.get(state)
            if entry is None:
                return False
            entry["result"] = {"value": value, "error": error}
            entry["event"].set()
        return True


class RemoteBrowserSigner:
    """Sign through the user's own browser wallet reached at a HOSTED HTTPS
    origin — for a hosted/remote MCP that can't open the user's *local* browser.

    It reuses `BrowserSigner`'s one-shot page and one-time state token, but the
    hosting app serves the page from ``base_url`` and decides how to send the
    user there via the ``deliver`` callback (e.g. an MCP url-mode elicitation).
    The hosting app must:
      - serve ``GET /sign?state=<token>`` -> :meth:`render_page`
      - serve ``POST /result?state=<token>`` -> :meth:`resolve`
    The URL carries only the opaque token; the tx/message stays server-side.
    Non-custodial: no fund-moving key lives here — the user's wallet signs.
    """

    fills_gas_and_nonce = True

    def __init__(
        self,
        *,
        base_url: str,
        deliver: Callable[[str], None],
        chain: Optional[dict[str, Any]] = None,
        timeout: float = 180.0,
    ) -> None:
        parsed = urlparse(base_url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" and host not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError(
                "base_url must be an https:// origin (the state token is a bearer "
                "capability); a localhost origin is allowed only for testing"
            )
        self._chain = chain
        self._bridge = _RemoteBridge(base_url, deliver, timeout)
        self._address: Optional[str] = None

    def _run(self, op: str, params: dict[str, Any]) -> Any:
        return self._bridge.request(op, {**params, "chain": self._chain})

    @property
    def address(self) -> str:
        if self._address is None:
            # Wallets return the address lowercased; SIWE requires EIP-55.
            self._address = to_checksum_address(self._run("connect", {}))
        return self._address

    def sign_message(self, message: str) -> str:
        return self._run("personal_sign", {"message": message, "address": self.address})

    def submit_transaction(self, web3: Any, transaction: dict[str, Any]) -> Any:
        tx = {
            "from": transaction["from"],
            "to": transaction["to"],
            "value": hex(transaction.get("value", 0)),
            "data": transaction.get("data") or "0x",
            "chainId": hex(transaction["chainId"]),
        }
        return HexBytes(self._run("send", {"tx": tx}))

    # --- hosting-app hooks -------------------------------------------------
    def render_page(self, state: str) -> str:
        """HTML for the pending op behind ``state`` (serve at GET /sign?state=)."""
        return self._bridge.render(state)

    def resolve(
        self, state: str, value: Any = None, error: Optional[str] = None
    ) -> bool:
        """Deliver the wallet's result for ``state`` and unblock the waiting call
        (call from POST /result?state=). Returns False for an unknown/expired
        token."""
        return self._bridge.resolve(state, value, error)
