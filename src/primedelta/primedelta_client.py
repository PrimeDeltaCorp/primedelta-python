import json
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Iterator, Optional, TypeVar
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from sseclient import SSEClient
from urllib3.util.retry import Retry

from primedelta.settings import PRIMEDELTA_BASE_URL, PYTH_HERMES_BASE_URL
from primedelta.types import (
    AccountStatus,
    AIAgent,
    AIAgentApproval,
    AIAgentPolicy,
    ApplicationSettings,
    BankDetails,
    ClaimableWithdrawal,
    DepositStocksSignature,
    DigitalIdentitySignature,
    Distribution,
    DistributionType,
    FiatWithdrawalBankAccount,
    InternalTransfer,
    InternalTransferKind,
    Message,
    Order,
    OrderCost,
    OrderSide,
    OrderStatus,
    PendingAIAgent,
    Portfolio,
    PortfolioHistory,
    Position,
    Price,
    Stock,
    TransactionType,
    Transfer,
    TransferHistoryStatus,
    WithdrawalSignature,
)

_STABLECOIN_SYMBOL = "dUSD"
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Default per-request timeout (seconds) so a hung/slow backend can never stall
# the caller indefinitely. Long-lived SSE streams (stream=True) are exempt.
_HTTP_TIMEOUT = 30.0
_STABLECOIN_DEPOSIT_DECIMALS = 2
_STOCKS_PAGE_SIZE = 100
_STOCKS_MAX_PAGES = 50
_BUSINESS_403_CODES = frozenset({"AGENT_PAUSED"})
_EnumT = TypeVar("_EnumT", bound=Enum)


def _enum_or_unknown(
    enum_type: type[_EnumT], value: str
) -> tuple[_EnumT, Optional[str]]:
    try:
        return enum_type(value), None
    except ValueError:
        return enum_type["UNKNOWN"], value


def _parse_datetime(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _decimal_arg(value: Decimal | int, name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise TypeError(f"{name} must be a Decimal or int, not {type(value).__name__}")
    number = Decimal(value)
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{name} must be a finite number > 0, got {value}")
    return format(number, "f")


def _cents_arg(value: Decimal | int, name: str) -> str:
    number = Decimal(_decimal_arg(value, name))
    cents = number.quantize(Decimal(1).scaleb(-_STABLECOIN_DEPOSIT_DECIMALS))
    if cents != number:
        raise ValueError(
            f"{name} must have at most {_STABLECOIN_DEPOSIT_DECIMALS} decimal "
            f"places (cents), got {format(number, 'f')}"
        )
    wire = format(number, "f")
    if len(wire.partition(".")[2]) > _STABLECOIN_DEPOSIT_DECIMALS:
        return format(cents, "f")
    return wire


def _stablecoin_deposit_amount(amount: Decimal | int) -> str:
    return _cents_arg(amount, "amount")


def _optional_cents_arg(value: Optional[Decimal | int], name: str) -> Optional[str]:
    return None if value is None else _cents_arg(value, name)


class _TimeoutSession(requests.Session):
    """A `requests.Session` that applies a default timeout to every request.

    All of `get`/`post`/`request`/`delete` funnel through `request`, so this
    covers every call site. Streaming requests (`stream=True`, i.e. the SSE
    price feeds) are left untouched — a read timeout would kill a long-lived
    stream. A per-call `timeout=` still overrides the default.
    """

    def __init__(self, timeout: float = _HTTP_TIMEOUT) -> None:
        super().__init__()
        self._timeout = timeout
        # Retry IDEMPOTENT requests over transient transport failures — a stale
        # keep-alive connection the server already closed (RemoteDisconnected),
        # a dropped connection, or a momentary 5xx/read-timeout — so a blip
        # opens a fresh connection and succeeds instead of surfacing a raw
        # traceback. POST/PUT/PATCH/DELETE are NOT retried (could double-submit).
        retry = Retry(
            total=2,
            connect=2,
            read=2,
            status=2,
            backoff_factor=0.3,
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.mount("https://", adapter)
        self.mount("http://", adapter)

    def request(self, *args: Any, **kwargs: Any) -> requests.Response:
        if not kwargs.get("stream"):
            kwargs.setdefault("timeout", self._timeout)
        return super().request(*args, **kwargs)


class NotLoggedIn(Exception):
    pass


class AuthorizationError(Exception):
    pass


class APIError(Exception):
    def __init__(
        self,
        error_code: str,
        message: Optional[str] = None,
        detail: Any = None,
        payload: Optional[dict[str, Any]] = None,
    ):
        self.error_code = error_code
        self.message = message
        self.detail = detail
        self.payload = payload if payload is not None else {}
        reason = message if message is not None else detail
        super().__init__(error_code if reason is None else f"{error_code}: {reason}")


class UserSignedMessageVerificationError(Exception):
    pass


class BackendUnavailable(Exception):
    """The backend could not be reached, or errored transiently — a dropped
    connection, a timeout that survived the automatic retries, or a 5xx. Kept
    distinct from `NotLoggedIn` / `AuthorizationError` / `APIError` so a caller
    (or the MCP layer) can back off and retry rather than crash on a raw
    `requests` traceback."""

    pass


class PrimeDeltaClient:
    def __init__(self, base_url: Optional[str] = None) -> None:
        self._session = _TimeoutSession()
        self._csrf_token: Optional[str] = None
        # Falls back to the module default so direct `PrimeDeltaClient()` use
        # (and tests that patch PRIMEDELTA_BASE_URL) keep working.
        self._base_url = base_url if base_url is not None else PRIMEDELTA_BASE_URL
        # Armed by the facade after a first successful login when auto-relogin is
        # on: on a 401 the client re-runs this once and retries (see _with_relogin).
        self._relogin: Optional[Callable[[], None]] = None
        self._relogging_in = False

    def set_relogin(self, callback: Optional[Callable[[], None]]) -> None:
        self._relogin = callback

    def _url(self, endpoint: str) -> str:
        return f"{self._base_url}{endpoint}"

    def _origin(self) -> str:
        parts = urlsplit(self._base_url)
        return f"{parts.scheme}://{parts.netloc}"

    def _session_get(self, url: str, **kwargs: Any) -> requests.Response:
        """A plain session GET (for endpoints that read the response directly),
        with transport failures mapped to the typed `BackendUnavailable` like
        `_request`. The session's retry adapter still handles transient blips
        first — this only types the error that survives the retries."""
        try:
            response = self._session.get(url, **kwargs)
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
        ) as exc:
            raise BackendUnavailable(str(exc)) from exc
        if response.status_code >= 500:
            raise BackendUnavailable(f"backend returned HTTP {response.status_code}")
        return response

    def _ensure_csrf_token(self) -> str:
        if self._csrf_token is None:
            response = self._session_get(self._url("/csrf-token/"))
            response.raise_for_status()
            self._csrf_token = response.json()["csrfToken"]
        return self._csrf_token

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> requests.Response:
        headers: dict[str, str] = {}
        if method in _UNSAFE_METHODS:
            origin = self._origin()
            headers["X-CSRFToken"] = self._ensure_csrf_token()
            headers["Origin"] = origin
            headers["Referer"] = origin + "/"
        try:
            return self._session.request(
                method,
                self._url(endpoint),
                params=params,
                data=data,
                json=json_body,
                headers=headers,
            )
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
        ) as exc:
            raise BackendUnavailable(str(exc)) from exc

    @staticmethod
    def _error_body(response: requests.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    @staticmethod
    def _code(body: dict[str, Any]) -> Optional[str]:
        return body.get("errorCode") or body.get("code")

    @staticmethod
    def _error_code(response: requests.Response) -> Optional[str]:
        return PrimeDeltaClient._code(PrimeDeltaClient._error_body(response))

    @staticmethod
    def _decimal_or_none(value: Optional[str]) -> Optional[Decimal]:
        return Decimal(value) if value is not None else None

    @classmethod
    def _is_business_refusal(cls, response: requests.Response) -> bool:
        return (
            response.status_code == 403
            and cls._error_code(response) in _BUSINESS_403_CODES
        )

    def _handle(self, response: requests.Response) -> Any:
        if response.status_code >= 500:
            raise BackendUnavailable(f"backend returned HTTP {response.status_code}")
        if response.status_code in (400, 404) or self._is_business_refusal(response):
            body = self._error_body(response)
            code = self._code(body)
            if code or response.status_code == 400:
                raise APIError(
                    code or "BAD_REQUEST",
                    body.get("message"),
                    body.get("detail"),
                    payload=body,
                )
        if response.status_code == 401:
            raise NotLoggedIn()
        if response.status_code == 403:
            raise AuthorizationError()
        response.raise_for_status()
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    def _send_with_relogin(
        self, send: Callable[[], requests.Response]
    ) -> requests.Response:
        # A 401 rejects the request before it executes, so re-running `send`
        # after re-authenticating never double-submits a mutation. Retry once,
        # returning the raw response so callers with custom body handling
        # (signed prices, DID) get keep-alive too. Best-effort single-flight:
        # a concurrent sibling that races the relogin window is not retried
        # (the SDK is single-instance-serial by design).
        response = send()
        if (
            response.status_code == 401
            and self._relogin is not None
            and not self._relogging_in
        ):
            self._relogging_in = True
            try:
                self._relogin()
            finally:
                self._relogging_in = False
            response = send()
        return response

    def _with_relogin(self, send: Callable[[], requests.Response]) -> Any:
        return self._handle(self._send_with_relogin(send))

    def _get(self, endpoint: str, params: Optional[dict[str, Any]] = None) -> Any:
        return self._with_relogin(lambda: self._request("GET", endpoint, params=params))

    def _unsafe(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        def send() -> requests.Response:
            response = self._request(method, endpoint, **kwargs)
            if (
                response.status_code == 403
                and self._csrf_token is not None
                and not self._is_business_refusal(response)
            ):
                self._csrf_token = None
                response = self._request(method, endpoint, **kwargs)
            return response

        return self._with_relogin(send)

    def _post(self, endpoint: str, json_body: dict[str, Any]) -> Any:
        return self._unsafe("POST", endpoint, json_body=json_body)

    def _put(self, endpoint: str, json_body: dict[str, Any]) -> Any:
        return self._unsafe("PUT", endpoint, json_body=json_body)

    def _delete(self, endpoint: str) -> Any:
        return self._unsafe("DELETE", endpoint)

    def get_nonce(self) -> str:
        response = self._session_get(self._url("/users/nonce/"))
        response.raise_for_status()
        return response.json()["nonce"]

    def login(self, message: str, signature: str, nonce: str) -> None:
        data = {"message": message, "signature": signature, "nonce": nonce}
        response = self._request("POST", "/users/verify/", data=data)
        if response.status_code == 403 and not self._is_business_refusal(response):
            self._csrf_token = None
            response = self._request("POST", "/users/verify/", data=data)
        if response.status_code == 400:
            if self._error_code(response) == "MESSAGE_VERIFICATION_ERROR":
                raise UserSignedMessageVerificationError()
        response.raise_for_status()
        self._csrf_token = None

    def logout(self) -> None:
        try:
            self._post("/logout/", {})
        finally:
            self._csrf_token = None
            self._session.cookies.clear()

    def export_session(self) -> list[dict[str, Any]]:
        """Serialize the session cookies for persistence (empty if not logged in)."""
        return [
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path,
                "secure": c.secure,
            }
            for c in self._session.cookies
        ]

    def import_session(self, cookies: list[dict[str, Any]]) -> None:
        """Restore cookies from `export_session`. Validity is not checked here — a
        stale session surfaces as `NotLoggedIn` on the next call (and, when
        auto-relogin is armed, is retried)."""
        self._session.cookies.clear()
        for c in cookies:
            self._session.cookies.set(
                c["name"],
                c["value"],
                domain=c.get("domain") or "",
                path=c.get("path") or "/",
                secure=bool(c.get("secure", False)),
            )
        self._csrf_token = None

    def me(self) -> str:
        return self._get("/me/")["address"]

    def get_account_status(self) -> AccountStatus:
        status, _ = _enum_or_unknown(
            AccountStatus, self._get("/verification-status/")["status"]
        )
        return status

    def register_ai_account(self, agent_name: str, main_wallet_address: str) -> None:
        self._post(
            "/register-ai-account/",
            {"agentName": agent_name, "mainWalletAddress": main_wallet_address},
        )

    def get_pending_ai_agents(self) -> list[PendingAIAgent]:
        return [
            PendingAIAgent(
                sub_wallet_address=item["subWalletAddress"],
                agent_name=item["agentName"],
            )
            for item in self._get("/pending-ai-agents/")
        ]

    def get_my_ai_agents(self) -> list[AIAgent]:
        return [self._parse_ai_agent(item) for item in self._get("/my-ai-agents/")]

    @classmethod
    def _parse_ai_agent(cls, item: dict[str, Any]) -> AIAgent:
        status, raw_status = _enum_or_unknown(AccountStatus, item["status"])
        return AIAgent(
            sub_wallet_address=item["subWalletAddress"],
            agent_name=item["agentName"],
            status=status,
            raw_status=raw_status,
            paused=item.get("paused"),
            allowed_symbols=item.get("allowedSymbols"),
            max_order_usd=cls._decimal_or_none(item.get("maxOrderUsd")),
            max_daily_usd=cls._decimal_or_none(item.get("maxDailyUsd")),
        )

    def get_ai_agent_portfolio(self, sub_wallet_address: str) -> Portfolio:
        return self._parse_portfolio(
            self._get("/ai-agent-portfolio/", {"subWalletAddress": sub_wallet_address})
        )

    def get_ai_agent_open_orders(
        self, sub_wallet_address: str, page: int, size: int
    ) -> list[Order]:
        response = self._get(
            "/ai-agent-open-orders/",
            {"subWalletAddress": sub_wallet_address, "page": page, "size": size},
        )
        return [self._parse_open_order(item) for item in response["items"]]

    def get_ai_agent_closed_orders(
        self, sub_wallet_address: str, page: int, size: int
    ) -> list[Order]:
        response = self._get(
            "/ai-agent-closed-orders/",
            {"subWalletAddress": sub_wallet_address, "page": page, "size": size},
        )
        return [self._parse_closed_order(item) for item in response["items"]]

    def close_ai_agent(self, sub_wallet_address: str) -> None:
        self._post("/close-ai-agent/", {"subWalletAddress": sub_wallet_address})

    def reopen_ai_agent(self, sub_wallet_address: str) -> None:
        self._post("/reopen-ai-agent/", {"subWalletAddress": sub_wallet_address})

    def get_ai_agent_policy(
        self, sub_wallet_address: Optional[str] = None
    ) -> AIAgentPolicy:
        params = (
            None
            if sub_wallet_address is None
            else {"subWalletAddress": sub_wallet_address}
        )
        return self._parse_ai_agent_policy(self._get("/ai-agent-policy/", params))

    def set_ai_agent_policy(
        self,
        sub_wallet_address: str,
        paused: bool,
        allowed_symbols: Optional[list[str]],
        max_order_usd: Optional[Decimal | int],
        max_daily_usd: Optional[Decimal | int],
    ) -> AIAgentPolicy:
        response = self._put(
            "/ai-agent-policy/",
            {
                "subWalletAddress": sub_wallet_address,
                "paused": paused,
                "allowedSymbols": allowed_symbols,
                "maxOrderUsd": _optional_cents_arg(max_order_usd, "max_order_usd"),
                "maxDailyUsd": _optional_cents_arg(max_daily_usd, "max_daily_usd"),
            },
        )
        return self._parse_ai_agent_policy(response)

    @classmethod
    def _parse_ai_agent_policy(cls, item: dict[str, Any]) -> AIAgentPolicy:
        return AIAgentPolicy(
            sub_wallet_address=item["subWalletAddress"],
            paused=item["paused"],
            allowed_symbols=item["allowedSymbols"],
            max_order_usd=cls._decimal_or_none(item["maxOrderUsd"]),
            max_daily_usd=cls._decimal_or_none(item["maxDailyUsd"]),
            day=date.fromisoformat(item["day"]),
            used_today_usd=Decimal(item["usedTodayUsd"]),
            remaining_today_usd=cls._decimal_or_none(item["remainingTodayUsd"]),
            resets_at=_parse_datetime(item["resetsAt"]),
        )

    def request_ai_agent_approval(
        self, sub_wallet_address: str, agent_name: Optional[str] = None
    ) -> AIAgentApproval:
        body = {"subWalletAddress": sub_wallet_address}
        if agent_name is not None:
            body["agentName"] = agent_name
        response = self._post("/request-ai-agent-approval/", body)
        return AIAgentApproval(
            nonce=response["nonce"],
            expires_at=_parse_datetime(response["expiresAt"]),
            main_message=response["mainMessage"],
            agent_message=response["agentMessage"],
        )

    def confirm_ai_agent(
        self,
        sub_wallet_address: str,
        nonce: Optional[str] = None,
        signature: Optional[str] = None,
    ) -> None:
        body = {"subWalletAddress": sub_wallet_address}
        if nonce is not None:
            body["nonce"] = nonce
        if signature is not None:
            body["signature"] = signature
        self._post("/confirm-ai-agent/", body)

    def link_ai_agent(
        self,
        sub_wallet_address: str,
        nonce: str,
        main_signature: str,
        agent_signature: str,
    ) -> None:
        self._post(
            "/link-ai-agent/",
            {
                "subWalletAddress": sub_wallet_address,
                "nonce": nonce,
                "mainSignature": main_signature,
                "agentSignature": agent_signature,
            },
        )

    def reject_ai_agent(self, sub_wallet_address: str) -> None:
        self._post("/reject-ai-agent/", {"subWalletAddress": sub_wallet_address})

    def fund_ai_agent(
        self, sub_wallet_address: str, amount: Decimal | int, request_id: str
    ) -> InternalTransfer:
        response = self._post(
            "/fund-ai-agent/",
            {
                "subWalletAddress": sub_wallet_address,
                "amount": _decimal_arg(amount, "amount"),
                "requestId": request_id,
            },
        )
        return self._parse_internal_transfer(response)

    def return_to_main(
        self,
        amount: Decimal | int,
        request_id: str,
        sub_wallet_address: Optional[str] = None,
    ) -> InternalTransfer:
        body = {"amount": _decimal_arg(amount, "amount"), "requestId": request_id}
        if sub_wallet_address is not None:
            body["subWalletAddress"] = sub_wallet_address
        return self._parse_internal_transfer(self._post("/return-to-main/", body))

    @staticmethod
    def _parse_internal_transfer(item: dict[str, Any]) -> InternalTransfer:
        kind, raw_kind = _enum_or_unknown(InternalTransferKind, item["kind"])
        return InternalTransfer(
            transfer_id=item["transferId"],
            kind=kind,
            amount=Decimal(item["amount"]),
            symbol=item["symbol"],
            from_wallet_address=item["fromWalletAddress"],
            to_wallet_address=item["toWalletAddress"],
            created_at=_parse_datetime(item["createdAt"]),
            raw_kind=raw_kind,
        )

    def get_pending_transfers(self, page: int, size: int) -> list[Transfer]:
        response = self._get("/pending-transfers/", {"page": page, "size": size})
        return [self._parse_transfer(item) for item in response["items"]]

    def get_closed_transfers(self, page: int, size: int) -> list[Transfer]:
        response = self._get("/closed-transfers/", {"page": page, "size": size})
        return [self._parse_transfer(item) for item in response["items"]]

    @staticmethod
    def _parse_transfer(item: dict[str, Any]) -> Transfer:
        transfer_type, raw_type = _enum_or_unknown(TransactionType, item["type"])
        status, raw_status = _enum_or_unknown(TransferHistoryStatus, item["status"])
        return Transfer(
            transaction_id=item["transactionId"],
            amount=Decimal(item["amount"]),
            symbol=item["symbol"],
            type=transfer_type,
            status=status,
            transfer_id=item.get("transferId"),
            raw_type=raw_type,
            raw_status=raw_status,
        )

    def get_distributions(self, page: int, size: int) -> list[Distribution]:
        response = self._get("/closed-distributions/", {"page": page, "size": size})
        return [self._parse_distribution(item) for item in response["items"]]

    @staticmethod
    def _parse_distribution(item: dict[str, Any]) -> Distribution:
        distribution_type, raw_type = _enum_or_unknown(DistributionType, item["type"])
        return Distribution(
            amount=Decimal(item["amount"]),
            type=distribution_type,
            stock_symbol=item["stockSymbol"],
            stock_quantity=Decimal(item["quantity"]),
            raw_type=raw_type,
        )

    def create_digital_identity_signature(self) -> DigitalIdentitySignature:
        response = self._post(
            "/digital-identity-signature/", {"requestedFromLibrary": True}
        )
        return DigitalIdentitySignature(
            signature=response["signature"],
            nonce=response["nonce"],
            data=response["data"],
            is_pro=response["isPro"],
        )

    def cancel_order(self, order_id: int) -> None:
        self._delete(f"/open-orders/{order_id}/")

    def get_order_status(self, order_id: int) -> OrderStatus:
        return OrderStatus(self._get(f"/orders/{order_id}/status/")["orderStatus"])

    def open_orders(self, page: int, size: int) -> list[Order]:
        response = self._get("/open-orders/", {"page": page, "size": size})
        return [self._parse_open_order(item) for item in response["items"]]

    @staticmethod
    def _parse_open_order(item: dict[str, Any]) -> Order:
        return Order(
            id=item["id"],
            order_side=OrderSide(item["actionType"]),
            type=item["type"],
            symbol=item["stockSymbol"],
            quantity=Decimal(item["quantity"]),
            filled_quantity=Decimal(item["filledQuantity"]),
            price=Decimal(item["price"]),
            status=OrderStatus.PENDING,
            date_of_cancellation=(
                date.fromisoformat(item["dateOfCancellation"])
                if item["dateOfCancellation"]
                else None
            ),
        )

    def closed_orders(self, page: int, size: int) -> list[Order]:
        response = self._get("/closed-orders/", {"page": page, "size": size})
        return [self._parse_closed_order(item) for item in response["items"]]

    @staticmethod
    def _parse_closed_order(item: dict[str, Any]) -> Order:
        status, raw_status = _enum_or_unknown(OrderStatus, item["status"])
        return Order(
            id=item["id"],
            order_side=OrderSide(item["actionType"]),
            type=item["type"],
            symbol=item["stockSymbol"],
            quantity=Decimal(item["quantity"]),
            filled_quantity=Decimal(item["filledQuantity"]),
            price=Decimal(item["price"]) if item["price"] is not None else None,
            status=status,
            date_of_cancellation=(
                date.fromisoformat(item["dateOfCancellation"])
                if item["dateOfCancellation"]
                else None
            ),
            raw_status=raw_status,
        )

    def get_deposit_stocks_signature(
        self, amount: Decimal | int, symbol: str
    ) -> DepositStocksSignature:
        response = self._post(
            "/deposit-stocks-signature/",
            {"amount": _decimal_arg(amount, "amount"), "symbol": symbol},
        )
        return DepositStocksSignature(
            signature=response["signature"],
            nonce=response["nonce"],
            amount=response["amount"],
        )

    def get_deposit_stablecoin_signature(
        self, amount: Decimal | int, symbol: str
    ) -> DepositStocksSignature:
        response = self._post(
            "/deposit-stablecoin-signature/",
            {"amount": _stablecoin_deposit_amount(amount), "symbol": symbol},
        )
        return DepositStocksSignature(
            signature=response["signature"],
            nonce=response["nonce"],
            amount=response["amount"],
        )

    def request_stablecoin_withdrawal(self, amount: Decimal) -> int:
        response = self._post(
            "/initialize-stablecoin-withdraw/",
            {"amount": _decimal_arg(amount, "amount"), "symbol": _STABLECOIN_SYMBOL},
        )
        return response["withdrawalId"]

    def request_stock_withdrawal(self, amount: Decimal | int, asset_type: str) -> int:
        response = self._post(
            "/initialize-stocks-withdraw/",
            {"amount": _decimal_arg(amount, "amount"), "assetType": asset_type},
        )
        return response["withdrawalId"]

    def get_withdraw_signature(self, withdrawal_id: int) -> WithdrawalSignature:
        response = self._post(f"/withdraw-signature/{withdrawal_id}/", {})
        return WithdrawalSignature(
            signature=response["signature"],
            nonce=response["nonce"],
            amount=response["amount"],
        )

    def portfolio(self) -> Portfolio:
        return self._parse_portfolio(self._get("/portfolio/"))

    def _parse_portfolio(self, response: dict[str, Any]) -> Portfolio:
        balance = response["balance"]
        positions = response["stocks"]
        return Portfolio(
            buying_power=Decimal(balance["available"]),
            total_equity=Decimal(balance["equity"]),
            total_funds=Decimal(balance["funds"]),
            profit_loss=Decimal(balance["profitLoss"]),
            total_value=Decimal(balance["totalValue"]),
            positions=[
                Position(
                    symbol=stock["symbol"],
                    name=stock["name"],
                    total_owned=Decimal(stock["totalOwned"]),
                    available_to_sell=Decimal(stock["availableToSell"]),
                    average_purchase_price=Decimal(stock["averagePurchasePrice"]),
                    last_market_price=self._decimal_or_none(stock["lastMarketPrice"]),
                    profit_loss=Decimal(stock["profitLoss"]),
                    profit_loss_percentage=self._decimal_or_none(
                        stock["profitLossPercentage"]
                    ),
                    is_offboarded=stock["isOffboarded"],
                    multiplier_numerator=stock["multiplierNumerator"],
                    multiplier_denominator=stock["multiplierDenominator"],
                    quantity_decimals=stock.get("quantityDecimals"),
                    price_decimals=stock.get("priceDecimals"),
                )
                for stock in positions
            ],
        )

    def claimable_withdrawals(self) -> list[ClaimableWithdrawal]:
        response = self._get("/claimable-withdrawals/")
        return [
            ClaimableWithdrawal(
                withdrawal_id=item["withdrawalId"],
                amount=Decimal(item["amount"]),
                asset_type=item["assetType"],
            )
            for item in response["items"]
        ]

    def send_limit_order(
        self,
        amount: Decimal | int,
        asset_type: str,
        order_side: OrderSide,
        price_limit: Decimal,
        date_of_cancellation: Optional[date],
    ) -> int:
        request_data = {
            "amount": _decimal_arg(amount, "amount"),
            "stockSymbol": asset_type,
            "priceLimit": _decimal_arg(price_limit, "price_limit"),
            "dateOfCancellation": (
                str(date_of_cancellation) if date_of_cancellation is not None else None
            ),
        }
        response = self._post(
            f"/orders/limit/{order_side.value.lower()}/", request_data
        )
        return response["orderId"]

    def send_sell_market_order(self, amount: Decimal | int, asset_type: str) -> int:
        response = self._post(
            "/orders/market/sell/",
            {"amount": _decimal_arg(amount, "amount"), "stockSymbol": asset_type},
        )
        return response["orderId"]

    def stocks(self) -> dict[str, Stock]:
        stocks: dict[str, Stock] = {}
        seen = 0
        for page in range(1, _STOCKS_MAX_PAGES + 1):
            response = self._session_get(
                self._url("/stocks/"),
                params={"page": page, "size": _STOCKS_PAGE_SIZE},
            )
            response.raise_for_status()
            body = response.json()
            items = body["items"]
            for stock in items:
                stocks[stock["symbol"]] = Stock(
                    symbol=stock["symbol"],
                    name=stock["name"],
                    cusip=stock["cusipId"],
                    contract_address=stock["smartContractAddress"],
                    number_of_tokens_in_circulation=Decimal(stock["numberOfTokens"]),
                    quantity_decimals=stock.get("quantityDecimals"),
                    price_decimals=stock.get("priceDecimals"),
                )
            seen += len(items)
            total = body.get("total")
            if not items or not isinstance(total, int) or seen >= total:
                break
        return stocks

    def prices_stream_access_token(self) -> str:
        return self._get("/prices-stream-token/")["token"]

    def prices_stream(self, prices_stream_access_token: str) -> Iterator[Price]:
        for sse_message in SSEClient(
            self._url("/prices-stream/"),
            session=self._session,
            params={"token": prices_stream_access_token},
        ):
            price_data = json.loads(sse_message.data)
            yield Price(
                symbol=price_data["symbol"],
                last_price=Decimal(price_data["price"]),
                timestamp=self._parse_timestamp(price_data["timestamp"]),
                percentage_change=Decimal(price_data["percentageChange"]),
            )

    def is_market_open(self) -> bool:
        response = self._session_get(self._url("/market-status/"))
        response.raise_for_status()
        return response.json()["isMarketOpen"]

    def messages(self) -> list[Message]:
        return [
            Message(id=item["id"], content=item["content"])
            for item in self._get("/messages/")
        ]

    def mark_message_read(self, message_id: int) -> None:
        self._post(f"/messages/{message_id}/", {})

    def bank_details(self) -> BankDetails:
        response = self._get("/user/bank-details/")
        return BankDetails(
            beneficiary_name=response["beneficiaryName"],
            beneficiary_address=response["beneficiaryAddress"],
            reference_code=response["referenceCode"],
            bank_name=response["bankName"],
            bic=response["bic"],
            account_number=response["accountNumber"],
            transit_number=response["transitNumber"],
            institution_number=response["institutionNumber"],
            bank_address=response["bankAddress"],
        )

    def request_fiat_withdrawal(
        self, amount: Decimal, bank_account: FiatWithdrawalBankAccount
    ) -> int:
        response = self._post(
            "/fiat-withdrawals/",
            {
                "amount": _decimal_arg(amount, "amount"),
                "beneficiaryName": bank_account.beneficiary_name,
                "beneficiaryAddress": bank_account.beneficiary_address,
                "bankName": bank_account.bank_name,
                "accountNumber": bank_account.account_number,
                "transitNumber": bank_account.transit_number,
                "institutionNumber": bank_account.institution_number,
                "bic": bank_account.bic,
                "bankAddress": bank_account.bank_address,
            },
        )
        return response["withdrawalId"]

    def _order_cost(self, response: dict[str, Any]) -> OrderCost:
        return OrderCost(
            total=self._decimal_or_none(response["total"]),
            service_fee=self._decimal_or_none(response["serviceFee"]),
            service_fee_rate_percentage=self._decimal_or_none(
                response["serviceFeeRatePercentage"]
            ),
            last_price=self._decimal_or_none(response.get("lastPrice")),
        )

    def limit_order_cost(
        self,
        order_side: OrderSide,
        symbol: str,
        amount: Decimal | int,
        price_limit: Decimal,
    ) -> OrderCost:
        response = self._get(
            f"/orders/limit/{order_side.value.lower()}/cost/",
            {
                "amount": _decimal_arg(amount, "amount"),
                "priceLimit": _decimal_arg(price_limit, "price_limit"),
                "stockSymbol": symbol,
            },
        )
        return self._order_cost(response)

    def market_sell_cost(self, symbol: str, amount: Decimal | int) -> OrderCost:
        response = self._get(
            "/orders/market/sell/cost/",
            {"amount": _decimal_arg(amount, "amount"), "stockSymbol": symbol},
        )
        return self._order_cost(response)

    def swappable_symbols(self) -> list[str]:
        return self._get("/swappable-symbols/")

    def application_settings(self) -> ApplicationSettings:
        response = self._get("/application-settings/")
        return ApplicationSettings(
            portfolio_refresh_rate=response["portfolioRefreshRate"],
            buying_digital_identity_fee=Decimal(response["buyingDigitalIdentityFee"]),
        )

    def portfolio_history(self, history_range: str) -> PortfolioHistory:
        response = self._get("/portfolio/history/", {"range": history_range})
        return PortfolioHistory(
            range=response["range"],
            start_value=Decimal(response["startValue"]),
            end_value=Decimal(response["endValue"]),
            change=Decimal(response["change"]),
            change_percentage=self._decimal_or_none(response["changePercentage"]),
        )

    def digital_identity_id(self) -> Optional[int]:
        response = self._send_with_relogin(
            lambda: self._request("GET", "/digital-identity/")
        )
        if response.status_code == 404:
            return None
        return self._handle(response)["tokenId"]

    def touch_session(self) -> bool:
        response = self._request("GET", "/verification-status/")
        if response.status_code == 401:
            return False
        if response.status_code >= 500:
            raise BackendUnavailable(f"backend returned HTTP {response.status_code}")
        return True

    def get_signed_price_updates(self, symbols: list[str]) -> list[bytes]:
        if not symbols:
            return []
        response = self._send_with_relogin(
            lambda: self._request(
                "GET", "/signed-prices/", params={"symbols": ",".join(symbols)}
            )
        )
        if response.status_code == 401:
            raise NotLoggedIn()
        if response.status_code == 403:
            raise AuthorizationError()
        response.raise_for_status()
        return [
            bytes.fromhex(item["signature"].removeprefix("0x"))
            for item in response.json()
        ]

    @staticmethod
    def get_pyth_feed_ids(symbols: list[str]) -> dict[str, str]:
        if not PYTH_HERMES_BASE_URL:
            raise RuntimeError(
                "The public Pyth price stream is parked: the free Hermes "
                "endpoint shut down on 2026-07-31. Set PYTH_HERMES_BASE_URL "
                "to an authenticated endpoint to re-enable it, or log in "
                "with a verified account to use the signed price stream."
            )
        feed_ids = {}
        for symbol in symbols:
            response = requests.get(
                f"{PYTH_HERMES_BASE_URL}/v2/price_feeds",
                params={"query": symbol, "asset_type": "equity"},
                timeout=_HTTP_TIMEOUT,
            )
            response.raise_for_status()
            feeds = response.json()
            for feed in feeds:
                feed_symbol = feed.get("attributes", {}).get("symbol", "")
                base = feed.get("attributes", {}).get("base", "")
                if base == symbol and feed_symbol == f"Equity.US.{symbol}/USD":
                    feed_ids[symbol] = feed["id"]
                    break
        return feed_ids

    def pyth_prices_stream(self, symbols: list[str]) -> Iterator[Price]:
        feed_ids = self.get_pyth_feed_ids(symbols)
        if not feed_ids:
            return
        ids_param = "&".join(f"ids[]={fid}" for fid in feed_ids.values())
        stream_url = f"{PYTH_HERMES_BASE_URL}/v2/updates/price/stream?{ids_param}"
        id_to_symbol = {v: k for k, v in feed_ids.items()}
        for sse_message in SSEClient(stream_url):
            if not sse_message.data:
                continue
            try:
                data = json.loads(sse_message.data)
                parsed_prices = data.get("parsed", [])
                for price_data in parsed_prices:
                    feed_id = price_data.get("id", "")
                    symbol = id_to_symbol.get(feed_id)
                    if symbol and "price" in price_data:
                        price_info = price_data["price"]
                        raw_price = int(price_info["price"])
                        expo = int(price_info["expo"])
                        actual_price = Decimal(raw_price) * Decimal(10) ** expo
                        publish_time = price_info.get("publish_time", 0)
                        timestamp = datetime.fromtimestamp(
                            publish_time, tz=timezone.utc
                        )
                        yield Price(
                            symbol=symbol,
                            last_price=actual_price,
                            timestamp=timestamp,
                            percentage_change=Decimal(0),
                        )
            except (json.JSONDecodeError, KeyError, ValueError):
                continue

    @staticmethod
    def _parse_timestamp(timestamp: str) -> datetime:
        return datetime.fromisoformat(timestamp).replace(tzinfo=timezone.utc)
