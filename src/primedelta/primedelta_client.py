import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from urllib.parse import urlsplit

import requests
from sseclient import SSEClient

from primedelta.settings import PRIMEDELTA_BASE_URL, PYTH_HERMES_BASE_URL
from primedelta.types import (
    AccountStatus,
    ClaimableWithdrawal,
    DepositStocksSignature,
    DigitalIdentitySignature,
    Distribution,
    DistributionType,
    Order,
    OrderSide,
    OrderStatus,
    Portfolio,
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


class NotLoggedIn(Exception):
    pass


class AuthorizationError(Exception):
    pass


class APIError(Exception):
    def __init__(self, error_code: str):
        self.error_code = error_code
        super().__init__(error_code)


class UserSignedMessageVerificationError(Exception):
    pass


class PrimeDeltaClient:
    def __init__(self) -> None:
        self._session = requests.Session()
        self._csrf_token: Optional[str] = None

    def _url(self, endpoint: str) -> str:
        return f"{PRIMEDELTA_BASE_URL}{endpoint}"

    def _origin(self) -> str:
        parts = urlsplit(PRIMEDELTA_BASE_URL)
        return f"{parts.scheme}://{parts.netloc}"

    def _ensure_csrf_token(self) -> str:
        if self._csrf_token is None:
            response = self._session.get(self._url("/csrf-token/"))
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
        return self._session.request(
            method,
            self._url(endpoint),
            params=params,
            data=data,
            json=json_body,
            headers=headers,
        )

    @staticmethod
    def _error_code(response: requests.Response) -> Optional[str]:
        try:
            body = response.json()
        except ValueError:
            return None
        if isinstance(body, dict):
            return body.get("errorCode") or body.get("code")
        return None

    @staticmethod
    def _decimal_or_none(value: Optional[str]) -> Optional[Decimal]:
        return Decimal(value) if value is not None else None

    def _handle(self, response: requests.Response) -> dict:
        if response.status_code in (400, 404):
            code = self._error_code(response)
            if code or response.status_code == 400:
                raise APIError(code or "BAD_REQUEST")
        if response.status_code == 401:
            raise NotLoggedIn()
        if response.status_code == 403:
            raise AuthorizationError()
        response.raise_for_status()
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    def _get(self, endpoint: str, params: Optional[dict[str, Any]] = None) -> dict:
        return self._handle(self._request("GET", endpoint, params=params))

    def _unsafe(self, method: str, endpoint: str, **kwargs: Any) -> dict:
        response = self._request(method, endpoint, **kwargs)
        if response.status_code == 403 and self._csrf_token is not None:
            self._csrf_token = None
            response = self._request(method, endpoint, **kwargs)
        return self._handle(response)

    def _post(self, endpoint: str, json_body: dict) -> dict:
        return self._unsafe("POST", endpoint, json_body=json_body)

    def _delete(self, endpoint: str) -> dict:
        return self._unsafe("DELETE", endpoint)

    def get_nonce(self) -> str:
        response = self._session.get(self._url("/users/nonce/"))
        response.raise_for_status()
        return response.json()["nonce"]

    def login(self, message: str, signature: str, nonce: str) -> None:
        response = self._session.post(
            self._url("/users/verify/"),
            data={"message": message, "signature": signature, "nonce": nonce},
        )
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

    def me(self) -> str:
        return self._get("/me/")["address"]

    def get_account_status(self) -> AccountStatus:
        return AccountStatus(self._get("/verification-status/")["status"])

    def get_pending_transfers(self, page: int, size: int) -> list[Transfer]:
        response = self._get("/pending-transfers/", {"page": page, "size": size})
        return [self._parse_transfer(item) for item in response["items"]]

    def get_closed_transfers(self, page: int, size: int) -> list[Transfer]:
        response = self._get("/closed-transfers/", {"page": page, "size": size})
        return [self._parse_transfer(item) for item in response["items"]]

    @staticmethod
    def _parse_transfer(item: dict) -> Transfer:
        return Transfer(
            transaction_id=item["transactionId"],
            amount=Decimal(item["amount"]),
            symbol=item["symbol"],
            type=TransactionType(item["type"]),
            status=TransferHistoryStatus(item["status"]),
        )

    def get_distributions(self, page: int, size: int) -> list[Distribution]:
        response = self._get("/closed-distributions/", {"page": page, "size": size})
        return [
            Distribution(
                amount=Decimal(item["amount"]),
                type=DistributionType(item["type"]),
                stock_symbol=item["stockSymbol"],
                stock_quantity=Decimal(item["quantity"]),
            )
            for item in response["items"]
        ]

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
        return [
            Order(
                id=item["id"],
                order_side=OrderSide(item["actionType"]),
                type=item["type"],
                symbol=item["stockSymbol"],
                quantity=int(Decimal(item["quantity"])),
                price=Decimal(item["price"]),
                status=OrderStatus.PENDING,
                date_of_cancellation=(
                    date.fromisoformat(item["dateOfCancellation"])
                    if item["dateOfCancellation"]
                    else None
                ),
            )
            for item in response["items"]
        ]

    def closed_orders(self, page: int, size: int) -> list[Order]:
        response = self._get("/closed-orders/", {"page": page, "size": size})
        return [
            Order(
                id=item["id"],
                order_side=OrderSide(item["actionType"]),
                type=item["type"],
                symbol=item["stockSymbol"],
                quantity=int(Decimal(item["quantity"])),
                price=Decimal(item["price"]) if item["price"] is not None else None,
                status=OrderStatus(item["status"]),
                date_of_cancellation=(
                    date.fromisoformat(item["dateOfCancellation"])
                    if item["dateOfCancellation"]
                    else None
                ),
            )
            for item in response["items"]
        ]

    def get_deposit_stocks_signature(
        self, amount: int, symbol: str
    ) -> DepositStocksSignature:
        response = self._post(
            "/deposit-stocks-signature/", {"amount": str(amount), "symbol": symbol}
        )
        return DepositStocksSignature(
            signature=response["signature"],
            nonce=response["nonce"],
        )

    def request_stablecoin_withdrawal(self, amount: Decimal) -> int:
        response = self._post(
            "/initialize-stablecoin-withdraw/",
            {"amount": str(amount), "symbol": _STABLECOIN_SYMBOL},
        )
        return response["withdrawalId"]

    def request_stock_withdrawal(self, amount: int, asset_type: str) -> int:
        response = self._post(
            "/initialize-stocks-withdraw/",
            {"amount": str(amount), "assetType": asset_type},
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
        response = self._get("/portfolio/")
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
        amount: int,
        asset_type: str,
        order_side: OrderSide,
        price_limit: Decimal,
        date_of_cancellation: Optional[date],
    ) -> int:
        request_data = {
            "amount": str(amount),
            "stockSymbol": asset_type,
            "priceLimit": str(price_limit),
            "dateOfCancellation": (
                str(date_of_cancellation) if date_of_cancellation is not None else None
            ),
        }
        response = self._post(
            f"/orders/limit/{order_side.value.lower()}/", request_data
        )
        return response["orderId"]

    def send_sell_market_order(self, amount: int, asset_type: str) -> int:
        response = self._post(
            "/orders/market/sell/",
            {"amount": str(amount), "stockSymbol": asset_type},
        )
        return response["orderId"]

    def stocks(self) -> dict[str, Stock]:
        response = self._session.get(self._url("/stocks/"), params={"size": 100})
        response.raise_for_status()
        stocks_data = response.json()["items"]
        return {
            stock["symbol"]: Stock(
                symbol=stock["symbol"],
                name=stock["name"],
                cusip=stock["cusipId"],
                contract_address=stock["smartContractAddress"],
                number_of_tokens_in_circulation=Decimal(stock["numberOfTokens"]),
            )
            for stock in stocks_data
        }

    def prices_stream_access_token(self) -> str:
        return self._get("/prices-stream-token/")["token"]

    def prices_stream(self, prices_stream_access_token: str):
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
        response = self._session.get(self._url("/market-status/"))
        response.raise_for_status()
        return response.json()["isMarketOpen"]

    def get_signed_price_updates(self, symbols: list[str]) -> list[bytes]:
        if not symbols:
            return []
        response = self._request(
            "GET", "/signed-prices/", params={"symbols": ",".join(symbols)}
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

    def pyth_prices_stream(self, symbols: list[str]):
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
