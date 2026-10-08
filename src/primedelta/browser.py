import json
import os
import secrets
import selectors
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from eth_utils import to_checksum_address
from hexbytes import HexBytes


class BrowserSignerError(Exception):
    pass


_WALLET_HELPERS = """
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

async function maybeSwitchChain(provider, chain) {
  if (!chain) return;
  try {
    await provider.request({method: "wallet_switchEthereumChain", params: [{chainId: chain.chainId}]});
  } catch (e) {
    if (e && e.code === 4902) {
      await provider.request({method: "wallet_addEthereumChain", params: [chain]});
    } else { throw e; }
  }
}

async function perform(provider, op, params) {
  const accounts = await provider.request({method: "eth_requestAccounts"});
  await maybeSwitchChain(provider, params.chain);
  if (op === "connect") return accounts[0];
  const expected = String(params.address || (params.tx && params.tx.from) || "").toLowerCase();
  if (expected && !accounts.some((a) => String(a).toLowerCase() === expected)) {
    throw new Error("Switch your wallet to " + expected + ", the account this session signed in with");
  }
  if (op === "personal_sign") {
    return await provider.request({method: "personal_sign", params: [utf8ToHex(params.message), params.address]});
  }
  if (op === "send") {
    return await provider.request({method: "eth_sendTransaction", params: [params.tx]});
  }
  throw new Error("Unknown operation");
}
"""

_PAGE = (
    """<!doctype html>
<html><head><meta charset="utf-8"><title>PrimeDelta wallet</title></head>
<body style="font-family:system-ui;max-width:32rem;margin:4rem auto;text-align:center">
<h2>PrimeDelta</h2>
<p id="status">Connecting to your wallet…</p>
<script id="config" type="application/json">%(config)s</script>
<script>
const CONFIG = JSON.parse(document.getElementById("config").textContent);
"""
    + _WALLET_HELPERS.replace("%", "%%")
    + """
async function run() {
  const provider = await discoverProvider();
  if (!provider) throw new Error("No EIP-1193 wallet found");
  return await perform(provider, CONFIG.op, CONFIG.params);
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
)

_SESSION_PAGE = (
    """<!doctype html>
<html><head><meta charset="utf-8"><title>PrimeDelta signer</title></head>
<body style="font-family:system-ui;max-width:32rem;margin:4rem auto;text-align:center">
<h2>PrimeDelta</h2>
<p id="status">Looking for your wallet…</p>
<script id="config" type="application/json">%(config)s</script>
<script>
const CONFIG = JSON.parse(document.getElementById("config").textContent);
"""
    + _WALLET_HELPERS.replace("%", "%%")
    + """
const LABELS = {
  connect: "Connect your wallet in the wallet popup.",
  personal_sign: "Sign the PrimeDelta sign-in message in your wallet.",
  send: "Review and confirm the transaction in your wallet.",
};

async function serve() {
  const provider = await discoverProvider();
  if (!provider) {
    setStatus("No wallet in this browser. Open this page in the browser that has your wallet: " + location.href);
    return;
  }
  setStatus("Ready. Keep this tab open: PrimeDelta asks your wallet from here.");
  const query = "?session=" + encodeURIComponent(CONFIG.session);
  for (;;) {
    let response;
    const asked = Date.now();
    try {
      response = await fetch("/next" + query, {cache: "no-store"});
    } catch (err) {
      setStatus("PrimeDelta stopped. You can close this tab.");
      return;
    }
    if (response.status === 204) {
      if (Date.now() - asked < 1000) await new Promise((r) => setTimeout(r, 1000));
      continue;
    }
    if (!response.ok) {
      setStatus("This signing session has ended. You can close this tab.");
      return;
    }
    const job = await response.json();
    setStatus(LABELS[job.op] || "Waiting for your wallet…");
    let value = null;
    let error = null;
    try {
      value = await perform(provider, job.op, job.params);
    } catch (err) {
      error = (err && err.message) || String(err);
    }
    try {
      await fetch("/result" + query + "&state=" + encodeURIComponent(job.state), {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({value: value, error: error}),
      });
    } catch (err) {
      setStatus("PrimeDelta stopped. You can close this tab.");
      return;
    }
    setStatus(error ? "Last request failed: " + error + ". Waiting for the next one…" : "Done. Waiting for the next request…");
  }
}

serve();
</script>
</body></html>
"""
)

_PORT_ENV = "PRIMEDELTA_BROWSER_SIGNER_PORT"


class _QuietServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        self.allow_reuse_address = sys.platform != "win32"
        super().server_bind()

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass


def _peer_open(connection: Any) -> bool:
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(connection, selectors.EVENT_READ)
            if not selector.select(0):
                return True
        return bool(connection.recv(1, socket.MSG_PEEK))
    except (OSError, ValueError):
        return False


def _json_for_script(value: Any) -> str:
    return (
        json.dumps(value)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _render_page(op: str, params: dict[str, Any], state: str) -> str:
    return _PAGE % {
        "config": _json_for_script({"op": op, "params": params, "state": state})
    }


def _render_session_page(session: str) -> str:
    return _SESSION_PAGE % {"config": _json_for_script({"session": session})}


def _loopback_port(port: Optional[int]) -> int:
    if port is not None:
        return port
    raw = os.environ.get(_PORT_ENV, "").strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError:
        return 0
    return value if 0 <= value <= 65535 else 0


class _LoopbackBridge:
    poll_seconds = 20.0
    tab_grace_seconds = 3.0
    liveness_seconds = 1.0
    reopen_seconds = 15.0

    def __init__(self, timeout: float, port: int = 0) -> None:
        self._timeout = timeout
        self._requested_port = port
        self._session = secrets.token_urlsafe(32)
        self._cond = threading.Condition()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._queue: list[str] = []
        self._polling = 0
        self._last_poll = 0.0
        self._page_loaded = 0.0
        self._server: Optional[ThreadingHTTPServer] = None
        self._closed = False
        self._origin = ""
        self._host = ""

    @property
    def origin(self) -> str:
        self._start()
        return self._origin

    @property
    def page_url(self) -> str:
        return f"{self.origin}/?session={self._session}"

    def _start(self) -> None:
        with self._cond:
            if self._closed:
                raise BrowserSignerError("the wallet signer is closed")
            if self._server is not None:
                return
            server = self._bind(self._handler())
            port = server.server_address[1]
            self._server = server
            self._host = f"127.0.0.1:{port}"
            self._origin = f"http://{self._host}"
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def _bind(self, handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
        try:
            return _QuietServer(("127.0.0.1", self._requested_port), handler)
        except (OSError, OverflowError) as exc:
            if self._requested_port == 0:
                raise
            print(
                f"PrimeDelta: loopback port {self._requested_port} is unavailable "
                f"({exc}); using a random port for this session.",
                file=sys.stderr,
                flush=True,
            )
            return _QuietServer(("127.0.0.1", 0), handler)

    def close(self) -> None:
        with self._cond:
            server, self._server = self._server, None
            self._closed = True
            for job in self._jobs.values():
                if not job["done"].is_set():
                    job["closed"] = True
                    job["done"].set()
            self._queue.clear()
            self._cond.notify_all()
        if server is not None:
            server.shutdown()
            server.server_close()

    def _tab_busy(self) -> bool:
        return any(
            job["dispatched"] and not job["done"].is_set()
            for job in self._jobs.values()
        )

    def request(
        self, op: str, params: dict[str, Any], opener: Callable[[str], None]
    ) -> Any:
        self._start()
        state = secrets.token_urlsafe(16)
        done = threading.Event()
        job: dict[str, Any] = {
            "op": op,
            "params": params,
            "done": done,
            "dispatched": False,
            "closed": False,
            "result": None,
        }
        with self._cond:
            self._jobs[state] = job
            self._queue.append(state)
            tab_ready = (
                self._polling > 0
                or self._tab_busy()
                or time.monotonic() - self._last_poll < self.tab_grace_seconds
            )
            self._cond.notify_all()
        try:
            self._wait(job, tab_ready, opener)
        finally:
            with self._cond:
                self._jobs.pop(state, None)
                if state in self._queue:
                    self._queue.remove(state)
                finished = done.is_set()
        if job["closed"]:
            raise BrowserSignerError(
                "the wallet signer was closed"
                + (
                    "; the wallet may still complete the request, so check balances "
                    "and transactions before retrying"
                    if job["dispatched"]
                    else "; nothing was sent to the wallet"
                )
            )
        if not finished:
            if job["dispatched"]:
                raise BrowserSignerError(
                    "the wallet did not answer in time; it may still complete the "
                    "request, so check balances and transactions before retrying"
                )
            raise BrowserSignerError(
                "timed out waiting for the wallet tab; nothing was sent to the wallet"
            )
        payload = job["result"] or {}
        if payload.get("error"):
            raise BrowserSignerError(payload["error"])
        return payload.get("value")

    def _wait(
        self, job: dict[str, Any], tab_ready: bool, opener: Callable[[str], None]
    ) -> None:
        done = job["done"]
        opened_at: Optional[float] = None
        if not tab_ready:
            opened_at = time.monotonic()
            opener(self.page_url)
        reopened = False
        give_up = time.monotonic() + self._timeout
        while not done.is_set():
            now = time.monotonic()
            if now >= give_up:
                return
            if done.wait(min(self.liveness_seconds, give_up - now)):
                return
            now = time.monotonic()
            with self._cond:
                stranded = (
                    not job["dispatched"]
                    and self._polling == 0
                    and not self._tab_busy()
                    and now - self._last_poll >= self.tab_grace_seconds
                )
                loaded = opened_at is not None and self._page_loaded >= opened_at
            if reopened or not stranded or loaded:
                continue
            if opened_at is None or now - opened_at >= self.reopen_seconds:
                opener(self.page_url)
                reopened = True

    def _next_job(self, alive: Callable[[], bool]) -> Optional[dict[str, Any]]:
        deadline = time.monotonic() + self.poll_seconds
        with self._cond:
            self._polling += 1
            try:
                while True:
                    if self._server is None or not alive():
                        return None
                    if self._queue:
                        state = self._queue.pop(0)
                        job = self._jobs[state]
                        job["dispatched"] = True
                        return {
                            "state": state,
                            "op": job["op"],
                            "params": job["params"],
                        }
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._cond.wait(min(remaining, self.liveness_seconds))
            finally:
                self._polling -= 1
                self._last_poll = time.monotonic()

    def _requeue(self, state: str) -> None:
        with self._cond:
            job = self._jobs.get(state)
            if job is None:
                return
            job["dispatched"] = False
            self._queue.insert(0, state)
            self._cond.notify_all()

    def _resolve(self, state: Optional[str], payload: Any) -> bool:
        if not state or not isinstance(payload, dict):
            return False
        with self._cond:
            job = self._jobs.get(state)
            if job is None or not job["dispatched"] or job["done"].is_set():
                return False
            job["result"] = payload
            job["done"].set()
            self._last_poll = time.monotonic()
        return True

    def _authorized(self, host: Optional[str], session: Optional[str]) -> bool:
        return host == self._host and secrets.compare_digest(
            session or "", self._session
        )

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _reply(
                self, code: int, body: bytes = b"", content_type: str = ""
            ) -> None:
                self.send_response(code)
                self.send_header("Cache-Control", "no-store")
                if content_type:
                    self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _query(self) -> Optional[tuple[str, dict[str, list[str]]]]:
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                if not bridge._authorized(
                    self.headers.get("Host"), query.get("session", [None])[0]
                ):
                    self._reply(403)
                    return None
                return parsed.path, query

            def do_GET(self) -> None:
                found = self._query()
                if found is None:
                    return
                path, _ = found
                if path == "/":
                    with bridge._cond:
                        bridge._page_loaded = time.monotonic()
                    page = _render_session_page(bridge._session).encode()
                    self._reply(200, page, "text/html; charset=utf-8")
                elif path == "/next":
                    job = bridge._next_job(lambda: _peer_open(self.connection))
                    if job is None:
                        self._reply(204)
                        return
                    try:
                        self._reply(200, json.dumps(job).encode(), "application/json")
                    except OSError:
                        bridge._requeue(job["state"])
                        raise
                else:
                    self._reply(404)

            def do_POST(self) -> None:
                found = self._query()
                if found is None:
                    return
                path, query = found
                if path != "/result":
                    self._reply(404)
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    payload = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    self._reply(400)
                    return
                if bridge._resolve(query.get("state", [None])[0], payload):
                    self._reply(204)
                else:
                    self._reply(404)

        return Handler


class BrowserSigner:
    """Sign through a browser wallet (MetaMask, …) over a local loopback bridge.

    The first operation opens one page on 127.0.0.1; it discovers the wallet
    (EIP-6963), stays open and serves every later `eth_requestAccounts` /
    `personal_sign` / `eth_sendTransaction` from the same origin, so the wallet
    asks to connect once per session instead of once per operation. Pass `chain`
    (a `wallet_addEthereumChain` params dict) to switch/add the network before
    signing, and `port` (or `PRIMEDELTA_BROWSER_SIGNER_PORT`) to pin the loopback
    port so the wallet remembers the connection across restarts.
    """

    fills_gas_and_nonce = True

    def __init__(
        self,
        *,
        chain: Optional[dict[str, Any]] = None,
        timeout: float = 180.0,
        port: Optional[int] = None,
    ) -> None:
        self._chain = chain
        self._bridge = _LoopbackBridge(timeout, _loopback_port(port))
        self._address: Optional[str] = None

    @property
    def loopback_origin(self) -> str:
        return self._bridge.origin

    def close(self) -> None:
        self._bridge.close()

    def _open(self, url: str) -> None:
        # Always surface the URL so a user whose default browser has no wallet
        # (e.g. Safari without MetaMask) can paste it into the right one, and so
        # a retry after an error is copy-pasteable. Then best-effort auto-open.
        print(
            "\nPrimeDelta wallet: approve in a browser signed into your wallet "
            "(MetaMask / Rabby / …) and keep that tab open. If the wrong browser "
            f"opened or it has no wallet, paste this URL into the right one:\n"
            f"  {url}\n",
            file=sys.stderr,
            flush=True,
        )
        try:
            webbrowser.open(url)
        except Exception as exc:
            # Auto-open is best-effort; the URL was printed above to paste by hand.
            print(
                f"PrimeDelta: couldn't auto-open a browser ({exc}) — "
                "use the URL above.",
                file=sys.stderr,
                flush=True,
            )

    def _run(self, op: str, params: dict[str, Any]) -> Any:
        return self._bridge.request(op, {**params, "chain": self._chain}, self._open)

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
    """For a HOSTED origin: the hosting app serves the one-shot wallet page (the
    same wallet logic as the local signer tab) instead of a 127.0.0.1 server. A
    pending operation is
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

    It serves a one-shot wallet page (the same wallet logic as `BrowserSigner`'s
    tab) under a one-time state token, but the hosting app serves the page from
    ``base_url`` and decides how to send the user there via the ``deliver``
    callback (e.g. an MCP url-mode elicitation).
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
