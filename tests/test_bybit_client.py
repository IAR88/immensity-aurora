import hashlib
import hmac
import json
import unittest
from unittest.mock import Mock, patch

import requests

from engine.bybit_client import BybitAPIError, BybitClient


class BybitClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = Mock()
        self.client = BybitClient(
            "test-api-key",
            "test-api-secret",
            testnet=True,
            domain="bybit.com",
            session=self.session,
            min_request_interval=0,
            max_retries=0,
        )

    @staticmethod
    def response(payload: dict[str, object], status_code: int = 200) -> Mock:
        response = Mock()
        response.status_code = status_code
        response.headers = {}
        response.json.return_value = payload
        response.raise_for_status.return_value = None
        return response

    def instrument_response(self) -> Mock:
        return self.response(
            {
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "lotSizeFilter": {
                                "qtyStep": "0.001",
                                "minOrderQty": "0.001",
                                "minOrderAmt": "5",
                            },
                            "priceFilter": {"tickSize": "0.1"},
                        }
                    ]
                },
            }
        )

    def wallet_response(self, coin: str, available: str) -> Mock:
        return self.response(
            {
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "coin": [
                                {
                                    "coin": coin,
                                    "availableToWithdraw": available,
                                }
                            ]
                        }
                    ]
                },
            }
        )

    def test_requires_api_key_and_secret(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "BYBIT_API_KEY"):
                BybitClient()
            with self.assertRaisesRegex(ValueError, "BYBIT_API_SECRET"):
                BybitClient("configured-key")

    def test_domain_defaults_to_eu_and_environment_selects_rest_host(self) -> None:
        with patch.dict(
            "os.environ",
            {"BYBIT_TESTNET": "false"},
            clear=True,
        ):
            eu_client = BybitClient(
                "test-api-key",
                "test-api-secret",
                session=self.session,
                min_request_interval=0,
            )
        self.assertEqual(eu_client.base_url, "https://api.bybit.eu")

        with patch.dict(
            "os.environ",
            {"BYBIT_DOMAIN": "bybit.com", "BYBIT_TESTNET": "false"},
            clear=True,
        ):
            configured_client = BybitClient(
                "test-api-key",
                "test-api-secret",
                session=self.session,
                min_request_interval=0,
            )
        self.assertEqual(configured_client.base_url, "https://api.bybit.com")

    def test_environment_domain_is_used_for_actual_rest_request(self) -> None:
        self.session.request.return_value = self.wallet_response("USDT", "12.5")
        with patch.dict(
            "os.environ",
            {"BYBIT_DOMAIN": "bybit.eu", "BYBIT_TESTNET": "false"},
            clear=True,
        ):
            client = BybitClient(
                "test-api-key",
                "test-api-secret",
                session=self.session,
                min_request_interval=0,
            )
            self.assertEqual(client.get_wallet_balance(), 12.5)

        url = self.session.request.call_args.args[1]
        self.assertTrue(url.startswith("https://api.bybit.eu/v5/account/wallet-balance?"))

    def test_signed_headers_use_v5_hmac_sha256(self) -> None:
        self.session.request.return_value = self.wallet_response("USDT", "12.5")
        with patch("engine.bybit_client.time.time", return_value=1_700_000_000):
            balance = self.client.get_wallet_balance()

        self.assertEqual(balance, 12.5)
        kwargs = self.session.request.call_args.kwargs
        headers = kwargs["headers"]
        timestamp = str(1_700_000_000_000)
        message = f"{timestamp}test-api-key5000accountType=UNIFIED&coin=USDT"
        expected = hmac.new(
            b"test-api-secret", message.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        self.assertEqual(headers["X-BAPI-SIGN"], expected)
        self.assertEqual(headers["X-BAPI-API-KEY"], "test-api-key")

    def test_market_order_uses_step_rounded_quantity_and_v5_order_endpoint(self) -> None:
        self.session.request.side_effect = [
            self.instrument_response(),
            self.response({"retCode": 0, "result": {"orderId": "order-1"}}),
        ]

        result = self.client.execute_order("BTCUSDT", "Buy", 0.1239)

        self.assertEqual(result["result"]["orderId"], "order-1")
        method, url = self.session.request.call_args.args[:2]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/v5/order/create"))
        body = json.loads(self.session.request.call_args.kwargs["data"])
        self.assertEqual(body["qty"], "0.123")
        self.assertEqual(body["orderType"], "Market")
        self.assertEqual(body["marketUnit"], "baseCoin")

    def test_limit_order_rounds_price_to_tick_and_quantity_to_lot(self) -> None:
        self.session.request.side_effect = [
            self.instrument_response(),
            self.response({"retCode": 0, "result": {"orderId": "order-2"}}),
        ]

        self.client.execute_order("BTCUSDT", "Sell", 0.1239, "Limit", price=100.29)

        body = json.loads(self.session.request.call_args.kwargs["data"])
        self.assertEqual(body["qty"], "0.123")
        self.assertEqual(body["price"], "100.2")
        self.assertEqual(body["timeInForce"], "GTC")

    def test_rejects_nonzero_ret_code(self) -> None:
        self.session.request.return_value = self.response(
            {"retCode": 10001, "retMsg": "invalid request"}
        )
        with self.assertRaisesRegex(BybitAPIError, "10001"):
            self.client.get_wallet_balance()

    def test_retries_http_429_for_safe_reads(self) -> None:
        rate_limited = self.response({"retCode": 0}, status_code=429)
        rate_limited.raise_for_status.side_effect = requests.HTTPError("429")
        succeeded = self.wallet_response("USDT", "5")
        self.session.request.side_effect = [rate_limited, succeeded]
        self.client.max_retries = 1
        with patch("engine.bybit_client.time.sleep"):
            self.assertEqual(self.client.get_wallet_balance(), 5)
        self.assertEqual(self.session.request.call_count, 2)

    def test_retries_transient_network_failure_for_safe_reads(self) -> None:
        self.session.request.side_effect = [
            requests.ConnectionError("temporary network failure"),
            self.wallet_response("USDT", "7"),
        ]
        self.client.max_retries = 1
        with patch("engine.bybit_client.time.sleep"):
            self.assertEqual(self.client.get_wallet_balance(), 7)
        self.assertEqual(self.session.request.call_count, 2)

    def test_does_not_retry_ambiguous_order_submission(self) -> None:
        self.session.request.side_effect = [
            self.instrument_response(),
            requests.ConnectionError("connection dropped"),
        ]
        with self.assertRaisesRegex(BybitAPIError, "network request failed"):
            self.client.execute_order("BTCUSDT", "Buy", 0.1)
        self.assertEqual(self.session.request.call_count, 2)

    def test_close_position_sells_available_base_quantity(self) -> None:
        self.session.request.side_effect = [
            self.wallet_response("BTC", "0.0129"),
            self.instrument_response(),
            self.response({"retCode": 0, "result": {"orderId": "close-1"}}),
        ]

        self.client.close_position("BTCUSDT")

        body = json.loads(self.session.request.call_args.kwargs["data"])
        self.assertEqual(body["side"], "Sell")
        self.assertEqual(body["qty"], "0.012")
        self.assertEqual(body["orderType"], "Market")

    def test_mainnet_orders_require_explicit_opt_in(self) -> None:
        client = BybitClient(
            "test-api-key",
            "test-api-secret",
            testnet=False,
            domain="bybit.com",
            session=self.session,
            min_request_interval=0,
            max_retries=0,
        )
        with self.assertRaisesRegex(RuntimeError, "Mainnet orders are disabled"):
            client.execute_order("BTCUSDT", "Buy", 0.01)
        self.session.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
