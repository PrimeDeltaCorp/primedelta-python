from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from primedelta import NotEnoughFunds, PrimeDelta
from primedelta.primedelta_client import APIError
from primedelta.types import OrderSide


def _pd():
    with patch("primedelta.primedelta.Web3"):
        pd = PrimeDelta(private_key="0x" + "1" * 64, web3_provider_url="http://x")
    pd._primedelta_client = MagicMock()
    return pd


class TestFacadeFeatures:
    def test_request_fiat_withdrawal_maps_insufficient_funds(self):
        pd = _pd()
        pd._primedelta_client.request_fiat_withdrawal.side_effect = APIError(
            "INSUFFICIENT_FUNDS"
        )
        with pytest.raises(NotEnoughFunds):
            pd.request_fiat_withdrawal(Decimal("1"))

    def test_request_fiat_withdrawal_reraises_other_api_errors(self):
        pd = _pd()
        pd._primedelta_client.request_fiat_withdrawal.side_effect = APIError("OTHER")
        with pytest.raises(APIError):
            pd.request_fiat_withdrawal(Decimal("1"))

    def test_limit_buy_cost_delegates_with_buy_side(self):
        pd = _pd()
        pd.limit_buy_cost("AAPL", 1, Decimal("100"))
        args = pd._primedelta_client.limit_order_cost.call_args.args
        assert args[0] == OrderSide.BUY
        assert args[1] == "AAPL"

    def test_limit_sell_cost_delegates_with_sell_side(self):
        pd = _pd()
        pd.limit_sell_cost("AAPL", 1, Decimal("100"))
        assert (
            pd._primedelta_client.limit_order_cost.call_args.args[0] == OrderSide.SELL
        )

    def test_digital_identity_id_delegates(self):
        pd = _pd()
        pd._primedelta_client.digital_identity_id.return_value = 47
        assert pd.digital_identity_id() == 47
