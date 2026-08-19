from decimal import Decimal
from unittest.mock import MagicMock

import pytest
import requests

from primedelta.primedelta_client import (
    APIError,
    AuthorizationError,
    NotLoggedIn,
    PrimeDeltaClient,
    UserSignedMessageVerificationError,
)

_UNSET = object()


class _Resp:
    def __init__(self, status_code=200, json_data=_UNSET, content=None):
        self.status_code = status_code
        self._json = json_data
        if content is not None:
            self.content = content
        elif json_data is _UNSET:
            self.content = b""
        else:
            self.content = b'{"body": true}'
        self.headers: dict[str, str] = {}

    def json(self):
        if self._json is _UNSET:
            raise ValueError("no json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


def _client_with_session():
    client = PrimeDeltaClient()
    session = MagicMock()
    client._session = session
    return client, session


class TestLogin:
    def test_verify_posted_without_csrf_and_no_token_stored(self):
        client, session = _client_with_session()
        session.post.return_value = _Resp(204)

        client.login(message="m", signature="s", nonce="n")

        session.post.assert_called_once()
        args, kwargs = session.post.call_args
        assert args[0].endswith("/users/verify/")
        assert kwargs["data"] == {"message": "m", "signature": "s", "nonce": "n"}
        assert "headers" not in kwargs or "X-CSRFToken" not in (
            kwargs.get("headers") or {}
        )
        session.get.assert_not_called()
        assert client._csrf_token is None

    def test_raises_on_message_verification_error(self):
        client, session = _client_with_session()
        session.post.return_value = _Resp(
            400, {"errorCode": "MESSAGE_VERIFICATION_ERROR"}
        )

        with pytest.raises(UserSignedMessageVerificationError):
            client.login(message="m", signature="bad", nonce="n")


class TestCsrf:
    def test_unsafe_request_attaches_csrf_origin_referer_and_body(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(200, {"withdrawalId": 7})

        withdrawal_id = client.request_stablecoin_withdrawal(Decimal("10"))

        assert withdrawal_id == 7
        session.get.assert_any_call(client._url("/csrf-token/"))
        method, url = session.request.call_args.args
        kwargs = session.request.call_args.kwargs
        assert method == "POST"
        assert url.endswith("/initialize-stablecoin-withdraw/")
        headers = kwargs["headers"]
        assert headers["X-CSRFToken"] == "tok"
        assert headers["Origin"] == client._origin()
        assert headers["Referer"] == client._origin() + "/"
        assert kwargs["json"] == {"amount": "10", "symbol": "dUSD"}

    def test_get_request_carries_no_csrf(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(200, {"items": []})

        client.claimable_withdrawals()

        session.get.assert_not_called()
        headers = session.request.call_args.kwargs["headers"]
        assert "X-CSRFToken" not in headers

    def test_csrf_token_fetched_once_and_cached(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(200, {"withdrawalId": 1})

        client.request_stablecoin_withdrawal(Decimal("1"))
        client.request_stablecoin_withdrawal(Decimal("2"))

        csrf_calls = [
            c
            for c in session.get.call_args_list
            if c.args and c.args[0].endswith("/csrf-token/")
        ]
        assert len(csrf_calls) == 1


class TestErrorMapping:
    def test_401_raises_not_logged_in(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(
            401, {"detail": ["x"], "code": "NOT_AUTHENTICATED"}
        )
        with pytest.raises(NotLoggedIn):
            client.me()

    def test_403_raises_authorization_error(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(
            403, {"detail": ["x"], "code": "PERMISSION_DENIED"}
        )
        with pytest.raises(AuthorizationError):
            client.me()

    def test_400_business_errorcode_preserved(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(400, {"errorCode": "INSUFFICIENT_FUNDS"})
        with pytest.raises(APIError) as info:
            client.portfolio()
        assert info.value.error_code == "INSUFFICIENT_FUNDS"

    def test_400_drf_shape_falls_back_to_code(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(400, {"detail": ["x"], "code": "PARSE"})
        with pytest.raises(APIError) as info:
            client.portfolio()
        assert info.value.error_code == "PARSE"


class TestReads:
    def test_me_returns_address(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(200, {"address": "0xABC"})
        assert client.me() == "0xABC"

    def test_get_account_status_maps_verified_minted(self):
        from primedelta.types import AccountStatus

        client, session = _client_with_session()
        session.request.return_value = _Resp(200, {"status": "VERIFIED_MINTED"})
        assert client.get_account_status() == AccountStatus.DID_MINTED

    def test_portfolio_handles_null_profit_loss_percentage(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(
            200,
            {
                "balance": {
                    "available": "10",
                    "equity": "20",
                    "funds": "10",
                    "profitLoss": "0",
                    "totalValue": "30",
                },
                "stocks": [
                    {
                        "symbol": "AMMT1",
                        "name": "AMM Test 1",
                        "totalOwned": "20",
                        "availableToSell": "20",
                        "averagePurchasePrice": "0",
                        "lastMarketPrice": "10",
                        "profitLoss": "0",
                        "profitLossPercentage": None,
                        "isOffboarded": False,
                        "multiplierNumerator": 1,
                        "multiplierDenominator": 1,
                    }
                ],
            },
        )
        portfolio = client.portfolio()
        assert portfolio.positions[0].profit_loss_percentage is None

    def test_prices_stream_token_minted(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(200, {"token": "abc"})
        assert client.prices_stream_access_token() == "abc"
