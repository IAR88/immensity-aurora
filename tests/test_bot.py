import os
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import bot


class FakeSession:
    def __init__(self, positions=None):
        self.positions = positions or []
        self.orders = []

    def get_positions(self, **_):
        return {"retCode": 0, "result": {"list": self.positions}}

    def place_order(self, **order):
        self.orders.append(order)
        return {"retCode": 0, "result": {"orderId": "test-order"}}


def make_test_config(trading_enabled=False):
    return bot.BotConfig(
        symbols=("BTCUSDT",),
        trading_enabled=trading_enabled,
        testnet=True,
    )


class BotStrategyTests(unittest.TestCase):
    def test_environment_defaults_to_testnet(self):
        with patch.dict(os.environ, {}, clear=True):
            config = bot.BotConfig.from_environment()

        self.assertTrue(config.testnet)

    def test_session_accepts_bybit_secret_alias(self):
        config = make_test_config()
        http = Mock()
        with (
            patch.dict(
                os.environ,
                {"BYBIT_API_KEY": "test-key", "BYBIT_SECRET": "test-secret"},
                clear=True,
            ),
            patch.dict(
                sys.modules,
                {"pybit.unified_trading": SimpleNamespace(HTTP=http)},
            ),
        ):
            bot.create_bybit_session(config)

        http.assert_called_once_with(
            testnet=True,
            domain="bybit",
            tld="eu",
            api_key="test-key",
            api_secret="test-secret",
        )

    def test_entry_signal_requires_cross_and_rsi_confirmation(self):
        self.assertEqual(bot.entry_signal(10, 11, 12, 11, 60), "Buy")
        self.assertEqual(bot.entry_signal(11, 10, 9, 10, 40), "Sell")
        self.assertIsNone(bot.entry_signal(10, 11, 12, 11, 75))
        self.assertIsNone(bot.entry_signal(10, 11, 10, 11, 60))

    def test_risk_sizing_is_capped_by_available_margin(self):
        quantity = bot.size_position(
            equity=10_000,
            available_balance=500,
            entry_price=100,
            stop_price=95,
            quantity_step="0.001",
            minimum_quantity=0.001,
            minimum_notional=5,
            risk_per_trade=0.01,
            max_leverage=1,
        )
        self.assertEqual(quantity, bot.Decimal("5.000"))

    def test_opposite_signal_closes_existing_position_reduce_only(self):
        session = FakeSession(
            [{"side": "Buy", "size": "0.01", "positionIdx": 0}]
        )
        market = bot.MarketSnapshot(1000, 100, 99, 100, 101, 100, 40, 2, "Sell")
        with patch("bot.fetch_market_snapshot", return_value=market):
            bot.process_market_check(
                session,
                "BTCUSDT",
                make_test_config(trading_enabled=True),
                {},
                set(),
            )
        self.assertEqual(len(session.orders), 1)
        self.assertEqual(session.orders[0]["side"], "Sell")
        self.assertTrue(session.orders[0]["reduceOnly"])
        self.assertEqual(session.orders[0]["positionIdx"], 0)

    def test_order_submission_is_disabled_by_default(self):
        session = FakeSession()
        market = bot.MarketSnapshot(1000, 100, 99, 100, 101, 100, 60, 2, "Buy")
        with patch("bot.fetch_market_snapshot", return_value=market):
            bot.process_market_check(
                session,
                "BTCUSDT",
                make_test_config(),
                {},
                set(),
            )
        self.assertEqual(session.orders, [])


if __name__ == "__main__":
    unittest.main()