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
from primedelta.types import OrderSide

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


class TestAuditFixes:
    def test_account_status_accepts_new_kyc_states(self):
        from primedelta.types import AccountStatus

        client, session = _client_with_session()
        for value, expected in [
            ("ON_HOLD", AccountStatus.ON_HOLD),
            ("RESUBMISSION_REQUESTED", AccountStatus.RESUBMISSION_REQUESTED),
            ("REJECTED_FINAL", AccountStatus.REJECTED_FINAL),
            ("INVALID", AccountStatus.INVALID),
        ]:
            session.request.return_value = _Resp(200, {"status": value})
            assert client.get_account_status() == expected

    def test_distributions_accepts_other_type(self):
        from primedelta.types import DistributionType

        client, session = _client_with_session()
        session.request.return_value = _Resp(
            200,
            {
                "items": [
                    {
                        "amount": "1",
                        "type": "OTHER",
                        "stockSymbol": "X",
                        "quantity": "1",
                    }
                ]
            },
        )
        assert client.get_distributions(1, 10)[0].type == DistributionType.OTHER

    def test_portfolio_handles_null_last_market_price(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(
            200,
            {
                "balance": {
                    "available": "0",
                    "equity": "0",
                    "funds": "0",
                    "profitLoss": "0",
                    "totalValue": "0",
                },
                "stocks": [
                    {
                        "symbol": "X",
                        "name": "X",
                        "totalOwned": "1",
                        "availableToSell": "1",
                        "averagePurchasePrice": "0",
                        "lastMarketPrice": None,
                        "profitLoss": "0",
                        "profitLossPercentage": None,
                        "isOffboarded": False,
                        "multiplierNumerator": 1,
                        "multiplierDenominator": 1,
                    }
                ],
            },
        )
        position = client.portfolio().positions[0]
        assert position.last_market_price is None
        assert position.profit_loss_percentage is None

    def test_404_with_error_code_maps_to_api_error(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(404, {"errorCode": "ORDER_NOT_FOUND"})
        with pytest.raises(APIError) as info:
            client.cancel_order(5)
        assert info.value.error_code == "ORDER_NOT_FOUND"

    def test_404_without_error_code_raises_http_error(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(404)
        with pytest.raises(requests.HTTPError):
            client.cancel_order(5)

    def test_stale_csrf_403_refetches_and_retries_once(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.side_effect = [
            _Resp(403, {"detail": ["csrf"], "code": "PERMISSION_DENIED"}),
            _Resp(200, {"withdrawalId": 9}),
        ]
        assert client.request_stablecoin_withdrawal(Decimal("1")) == 9
        assert session.request.call_count == 2
        csrf_gets = [
            c
            for c in session.get.call_args_list
            if c.args and c.args[0].endswith("/csrf-token/")
        ]
        assert len(csrf_gets) == 2

    def test_persistent_403_raises_after_one_retry(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.side_effect = [
            _Resp(403, {"code": "PERMISSION_DENIED"}),
            _Resp(403, {"code": "PERMISSION_DENIED"}),
        ]
        with pytest.raises(AuthorizationError):
            client.request_stablecoin_withdrawal(Decimal("1"))
        assert session.request.call_count == 2

    def test_logout_clears_session_even_when_post_fails(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(403, {"code": "PERMISSION_DENIED"})
        with pytest.raises(AuthorizationError):
            client.logout()
        assert client._csrf_token is None
        session.cookies.clear.assert_called_once()


class TestAccountFeatures:
    def test_messages_parses_items(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(200, [{"id": 3, "content": "hi"}])
        messages = client.messages()
        assert messages[0].id == 3 and messages[0].content == "hi"

    def test_mark_message_read_posts_to_message(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(200)
        client.mark_message_read(7)
        method, url = session.request.call_args.args
        assert method == "POST" and url.endswith("/messages/7/")

    def test_bank_details_parses_nullable_fields(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(
            200,
            {
                "beneficiaryName": "X Ltd",
                "beneficiaryAddress": "somewhere",
                "referenceCode": "REF-1",
                "bankName": "A Bank",
                "bic": "AAAABBCC",
                "accountNumber": None,
                "transitNumber": None,
                "institutionNumber": None,
                "bankAddress": None,
            },
        )
        details = client.bank_details()
        assert details.reference_code == "REF-1"
        assert details.account_number is None

    def test_request_fiat_withdrawal_returns_id(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(200, {"withdrawalId": 12})
        assert client.request_fiat_withdrawal(Decimal("50")) == 12
        assert session.request.call_args.kwargs["json"] == {"amount": "50"}

    def test_limit_order_cost_parses(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(
            200,
            {
                "total": "101.00",
                "serviceFee": "1.00",
                "serviceFeeRatePercentage": "1.00",
            },
        )
        cost = client.limit_order_cost(OrderSide.BUY, "AAPL", 1, Decimal("100"))
        assert cost.total == Decimal("101.00")
        assert cost.last_price is None
        _, url = session.request.call_args.args
        assert url.endswith("/orders/limit/buy/cost/")

    def test_market_sell_cost_parses_last_price(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(
            200,
            {
                "total": "9.90",
                "serviceFee": "0.10",
                "serviceFeeRatePercentage": "1.00",
                "lastPrice": "10.00",
            },
        )
        cost = client.market_sell_cost("AAPL", 1)
        assert cost.last_price == Decimal("10.00")

    def test_swappable_symbols_returns_list(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(200, ["AAPL", "TSLA"])
        assert client.swappable_symbols() == ["AAPL", "TSLA"]

    def test_application_settings_parses(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(
            200, {"portfolioRefreshRate": 3000, "buyingDigitalIdentityFee": "0.00"}
        )
        settings = client.application_settings()
        assert settings.portfolio_refresh_rate == 3000
        assert settings.buying_digital_identity_fee == Decimal("0.00")

    def test_portfolio_history_parses(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(
            200,
            {
                "range": "7d",
                "startValue": "100",
                "endValue": "110",
                "change": "10",
                "changePercentage": "10.00",
            },
        )
        history = client.portfolio_history("7d")
        assert history.range == "7d" and history.change == Decimal("10")

    def test_digital_identity_id_returns_token(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(200, {"tokenId": 47})
        assert client.digital_identity_id() == 47

    def test_digital_identity_id_none_when_absent(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(404, {"detail": ["no did"]})
        assert client.digital_identity_id() is None
