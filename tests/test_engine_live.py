import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from pybit.unified_trading import HTTP

from auth.sessions_manager import SessionsManager, bybit_api_domain_parts
from engine_live import (
    AvanzaCashPosition,
    AvanzaPosition,
    CalibrationObservation,
    HjarnanLiveEngine,
    IsotonicCalibrator,
    LiveTradeSignal,
    ManagedSpotPosition,
    ProbabilityFeatures,
    TECHNICAL_SIGNALS,
    load_avanza_portfolio,
    mock_predictive_parameters,
)
from trade_analysis import TradeAnalyzer


class FakeBybitSession:
    testnet = True

    def __init__(self):
        self.orders = []

    def get_wallet_balance(self, **params):
        if params.get("accountType") != "UNIFIED" or params.get("coin") not in {
            "USDT",
            "BTC",
        }:
            raise AssertionError("Unexpected Bybit balance query.")
        return {
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "totalEquity": "1000",
                        "coin": [
                            {
                                "coin": "USDT",
                                "equity": "1000",
                                "availableToWithdraw": "1000",
                                "walletBalance": "1000",
                                "locked": "0",
                            }
                        ],
                    }
                ]
            },
        }

    def get_instruments_info(self, **params):
        if params.get("category") != "spot" or params.get("symbol") != "BTCUSDT":
            raise AssertionError("Unexpected Bybit instrument query.")
        return {
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "lotSizeFilter": {
                            "qtyStep": "0.0001",
                            "minOrderQty": "0.0001",
                            "minOrderAmt": "5",
                        },
                        "priceFilter": {"tickSize": "0.01"},
                    }
                ]
            },
        }

    def place_order(self, **order):
        self.orders.append(order)
        return {"retCode": 0, "result": {"orderId": "test-order"}}


class FakeAvanzaSession:
    def get_accounts_positions(self):
        return {
            "withOrderbook": [
                {
                    "instrument": {"orderbook": {"id": "AAA"}},
                    "volume": {"value": 2},
                    "value": {"value": 50},
                }
            ],
            "withoutOrderbook": [],
            "cashPositions": [],
        }


class FakeSessionsManager:
    def __init__(self):
        self.bybit = FakeBybitSession()
        self.avanza = FakeAvanzaSession()

    def get_bybit_session(self):
        return self.bybit

    def get_avanza_session(self):
        return self.avanza

    def get_bybit_credentials(self):
        return {
            "testnet": True,
            "api_key": "test-key",
            "api_secret": "test-secret",
        }


class LiveEngineTests(unittest.TestCase):
    def setUp(self):
        self.features = ProbabilityFeatures(0.5, 0.5, 0.5)
        self.observations = [
            CalibrationObservation(self.features, outcome)
            for outcome in (0, 1, 1, 1, 1)
        ]
        self.manager = FakeSessionsManager()
        self.engine = HjarnanLiveEngine(
            self.manager,
            daily_start_equity_usdt=1000,
            calibration_observations=self.observations,
        )
        self.trade_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.trade_directory.cleanup)
        self.engine.trade_analyzer = TradeAnalyzer(
            Path(self.trade_directory.name) / "trades.jsonl",
            on_trade_closed=Mock(),
        )
        probability = self.engine.probability_calibrator.predict(self.features.score())
        costs = 0.005
        expected_value = probability * 0.10 - (1 - probability) * 0.05 - costs
        self.signal = LiveTradeSignal(
            symbol="BTCUSDT",
            category="spot",
            side="Buy",
            entry_price=100,
            win_probability=probability,
            probability_features=self.features,
            take_profit_pct=0.10,
            stop_loss_pct=0.05,
            estimated_cost_pct=costs,
            expected_value_pct=expected_value,
            expected_value_verified=True,
            orderbook_bid_depth_usdt=2500,
            orderbook_ask_depth_usdt=1500,
            orderbook_verified=True,
            cvd=1,
            cvd_verified=True,
            technical_signals={key: True for key in TECHNICAL_SIGNALS},
            predictive_parameters=mock_predictive_parameters(),
        )

    def test_engine_uses_bybit_eu_api_endpoint(self):
        self.assertEqual(self.engine.bybit_endpoint, "https://api.bybit.eu")

    def test_http_403_switches_engine_to_mock_data_and_blocks_orders(self):
        error = requests.exceptions.HTTPError(
            "403 Forbidden", response=Mock(status_code=403)
        )
        with (
            patch.object(self.manager.bybit, "get_wallet_balance", side_effect=error),
            patch.dict("os.environ", {"MOCK_BYBIT_FREE_USDT": "321"}),
        ):
            self.assertEqual(self.engine._account_equity(), 321)

        self.assertTrue(self.engine.mock_mode)
        self.assertTrue(self.engine.bybit_ip_restricted)
        self.assertEqual(self.engine._available_balance("USDT"), 321)
        with self.assertRaisesRegex(RuntimeError, "cannot submit"):
            self.engine.execute_bybit_limit_trade(self.signal)

    def test_session_manager_defaults_bybit_to_testnet(self):
        with (
            patch.dict(
                "os.environ",
                {"BYBIT_API_KEY": "test-key", "BYBIT_SECRET": "test-secret"},
                clear=True,
            ),
            patch("auth.sessions_manager.load_dotenv"),
        ):
            session = SessionsManager().get_bybit_session()
        self.assertTrue(session.testnet)

    def test_eu_domain_resolves_to_pybit_european_hostname(self):
        domain, tld = bybit_api_domain_parts("bybit.eu")
        session = HTTP(
            testnet=False,
            domain=domain,
            tld=tld,
        )
        self.assertEqual(session.endpoint, "https://api.bybit.eu")

    def test_mock_mode_initializes_offline_sessions_without_credentials(self):
        with (
            patch.dict("os.environ", {"MOCK_MODE": "true"}, clear=True),
            patch("auth.sessions_manager.load_dotenv"),
        ):
            sessions = SessionsManager()
            bybit = sessions.get_bybit_session()
            avanza = sessions.get_avanza_session()
        self.assertTrue(bybit.testnet)
        self.assertEqual(bybit.get_wallet_balance(accountType="UNIFIED", coin="USDT")["retCode"], 0)
        self.assertEqual(avanza.get_accounts_positions()["withOrderbook"], [])
        with self.assertRaisesRegex(RuntimeError, "cannot submit orders"):
            bybit.place_order()

    def test_isotonic_regression_pools_violating_adjacent_scores(self):
        low = ProbabilityFeatures(0.2, 0.2, 0.2)
        high = ProbabilityFeatures(0.8, 0.8, 0.8)
        calibrator = IsotonicCalibrator(
            [
                CalibrationObservation(low, 1),
                CalibrationObservation(high, 0),
            ]
        )
        self.assertEqual(calibrator.predict(low.score()), 0.5)
        self.assertEqual(calibrator.predict(high.score()), 0.5)

    def test_valid_signal_recomputes_positive_expected_value(self):
        self.assertAlmostEqual(
            self.engine.validate_signal(self.signal),
            self.signal.expected_value_pct,
        )

    def test_unverified_or_mismatched_signal_is_rejected(self):
        with self.assertRaises(ValueError):
            self.engine.validate_signal(replace(self.signal, win_probability=0.81))
        with self.assertRaises(ValueError):
            self.engine.validate_signal(
                replace(self.signal, technical_signals={"P1": True})
            )
        with self.assertRaises(ValueError):
            self.engine.validate_signal(replace(self.signal, expected_value_pct=-0.01))
        with self.assertRaisesRegex(ValueError, "Order book imbalance"):
            self.engine.validate_signal(
                replace(
                    self.signal,
                    orderbook_bid_depth_usdt=1500,
                    orderbook_ask_depth_usdt=2500,
                )
            )

    def test_kelly_uses_quarter_and_half_fractional_sizing(self):
        self.assertAlmostEqual(
            self.engine.calculate_kelly_position_size(0.75, 1000, reward_to_risk=2),
            156.25,
        )
        self.assertAlmostEqual(
            self.engine.calculate_kelly_position_size(
                0.9, 1000, reward_to_risk=2, confluence=0.9
            ),
            850,
        )
        self.assertAlmostEqual(
            self.engine.calculate_kelly_position_size(
                0.999, 1000, reward_to_risk=2, confluence=1.0
            ),
            998.5,
        )

    def test_execution_requires_eighty_percent_and_complete_parameter_matrix(self):
        low_probability = replace(
            self.signal,
            win_probability=0.79,
            expected_value_pct=0.79 * 0.10 - 0.21 * 0.05 - 0.005,
        )
        with self.assertRaisesRegex(ValueError, "at least 80%"):
            self.engine.validate_signal(low_probability)

        saturated = mock_predictive_parameters()
        saturated["trend_saturation_level"] = 3
        with self.assertRaisesRegex(ValueError, "Level 3"):
            self.engine.validate_signal(
                replace(self.signal, predictive_parameters=saturated)
            )

    def test_drawdown_breaker_trips_at_two_percent(self):
        self.assertFalse(self.engine._check_daily_drawdown(980))
        self.assertTrue(self.engine._daily_halted)

    def test_breakeven_and_trailing_stop_only_move_up(self):
        self.assertEqual(
            self.engine.calculate_protective_stop(100, 104, 95),
            95,
        )
        self.assertEqual(
            self.engine.calculate_protective_stop(100, 105, 95),
            100,
        )
        self.assertEqual(
            self.engine.calculate_protective_stop(100, 110, 100),
            104.5,
        )
        updates = []
        self.engine.update_protective_stops(
            [ManagedSpotPosition("BTCUSDT", 100, 110, 100)],
            lambda symbol, stop: updates.append((symbol, stop)),
        )
        self.assertEqual(updates, [("BTCUSDT", 104.5)])

    def test_bybit_order_and_avanza_rotation_are_read_only(self):
        self.assertEqual(
            self.engine.execute_bybit_limit_trade(self.signal), "test-order"
        )
        order = self.manager.bybit.orders[0]
        self.assertTrue(order["orderLinkId"].startswith("ia"))
        self.assertEqual(order["timeInForce"], "PostOnly")
        self.assertEqual(float(order["takeProfit"]), 110)
        self.assertEqual(float(order["stopLoss"]), 95)
        positions_file = Path(self.trade_directory.name) / "avanza_positions.json"
        positions_file.write_text(
            json.dumps(self.manager.avanza.get_accounts_positions()),
            encoding="utf-8",
        )
        self.engine.avanza_positions_file = positions_file
        positions = self.engine.track_avanza_positions()
        self.assertEqual(positions, [AvanzaPosition("AAA", 2, 50)])
        candidates = self.engine.analyze_stock_rotation(positions, ["BBB"])
        self.assertEqual(
            [candidate.action for candidate in candidates], ["sell", "buy"]
        )
        self.assertFalse(hasattr(self.manager.avanza, "place_order"))

    def test_missing_avanza_positions_file_returns_empty_list(self):
        self.engine.avanza_positions_file = (
            Path(self.trade_directory.name) / "missing_positions.json"
        )
        self.assertEqual(self.engine.track_avanza_positions(), [])

    def test_avanza_snapshot_separates_cash_from_stock_holdings(self):
        positions_file = Path(self.trade_directory.name) / "avanza_positions.json"
        positions_file.write_text(
            json.dumps(
                {
                    "withOrderbook": [
                        {
                            "instrument": {
                                "orderbook": {
                                    "id": "AAA",
                                    "currency": "SEK",
                                }
                            },
                            "volume": {"value": 2},
                            "value": {"value": 50},
                        }
                    ],
                    "withoutOrderbook": [],
                    "cashPositions": [
                        {"balance": "1 500,25", "currency": "SEK"},
                        {"amount": {"value": 25.5}, "currencyCode": "EUR"},
                    ],
                }
            ),
            encoding="utf-8",
        )

        snapshot = load_avanza_portfolio(positions_file)

        self.assertTrue(snapshot.available)
        self.assertEqual(
            snapshot.positions, (AvanzaPosition("AAA", 2, 50, "SEK"),)
        )
        self.assertEqual(
            snapshot.cash_positions,
            (
                AvanzaCashPosition(1500.25, "SEK"),
                AvanzaCashPosition(25.5, "EUR"),
            ),
        )

    def test_missing_avanza_snapshot_is_unavailable_not_an_error(self):
        snapshot = load_avanza_portfolio(
            Path(self.trade_directory.name) / "not_created.json"
        )
        self.assertFalse(snapshot.available)
        self.assertEqual(snapshot.positions, ())
        self.assertEqual(snapshot.cash_positions, ())

    def test_liquid_funds_keep_avanza_cash_separate_from_stock_positions(self):
        positions_file = Path(self.trade_directory.name) / "avanza_positions.json"
        positions_file.write_text(
            json.dumps(
                {
                    "withOrderbook": [
                        {
                            "symbol": "AAA",
                            "quantity": 2,
                            "marketValue": 50_000,
                            "currency": "SEK",
                        }
                    ],
                    "cashPositions": [
                        {"balance": 500, "currency": "SEK"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.engine.avanza_positions_file = positions_file

        liquid_funds = self.engine.get_liquid_funds_by_source()

        self.assertEqual(liquid_funds, {"Bybit USDT": 1000, "Avanza SEK": 500})

    def test_ntfy_notifications_are_sent_only_when_portfolio_or_signals_change(self):
        self.engine.ntfy_topic = "private-topic"
        with patch.object(self.engine, "notify_ntfy") as notify:
            self.engine._notify_signal_changes([self.signal])
            self.engine._notify_signal_changes([self.signal])
            self.engine._notify_signal_changes(
                [replace(self.signal, entry_price=101)]
            )
            self.engine._notify_signal_changes([])

        self.assertEqual(notify.call_count, 3)
        self.assertEqual(
            [call.args[0] for call in notify.call_args_list],
            [
                "Trading signal detected",
                "Trading signal changed",
                "Trading signal cleared",
            ],
        )

        portfolio = load_avanza_portfolio(
            Path(self.trade_directory.name) / "missing_positions.json"
        )
        with patch.object(self.engine, "notify_ntfy") as notify:
            self.engine._notify_portfolio_sync(portfolio)
            self.engine._notify_portfolio_sync(portfolio)
        notify.assert_called_once_with(
            "Avanza portfolio unavailable",
            "The saved positions file is missing. No Avanza cash or holdings "
            "were included in the portfolio snapshot.",
        )

    def test_ntfy_delivery_uses_configured_topic_and_optional_authentication(self):
        self.engine.ntfy_topic = "private topic"
        self.engine.ntfy_server = "https://ntfy.example"
        self.engine.ntfy_access_token = "test-token"
        response = Mock()
        with patch("engine_live.requests.post", return_value=response) as post:
            self.engine.notify_ntfy("Portfolio sync", "Snapshot updated.")

        post.assert_called_once_with(
            "https://ntfy.example/private%20topic",
            data=b"Snapshot updated.",
            headers={
                "Title": "Portfolio sync",
                "Authorization": "Bearer test-token",
            },
            timeout=(2, 5),
        )
        response.raise_for_status.assert_called_once_with()

    def test_engine_order_link_tracks_fills_through_realized_close(self):
        with tempfile.TemporaryDirectory() as directory:
            callback = Mock()
            self.engine.trade_analyzer = TradeAnalyzer(
                Path(directory) / "trades.jsonl",
                on_trade_closed=callback,
            )
            self.engine.execute_bybit_limit_trade(self.signal)
            order = self.manager.bybit.orders[-1]
            order_link_id = order["orderLinkId"]
            quantity = order["qty"]

            self.engine.trade_analyzer.record_execution(
                {
                    "data": [
                        {
                            "category": "spot",
                            "execId": "entry-exec",
                            "symbol": "BTCUSDT",
                            "side": "Buy",
                            "execQty": quantity,
                            "execPrice": order["price"],
                            "execFee": "0",
                            "feeCurrency": "USDT",
                            "execTime": "1760000000000",
                            "orderLinkId": order_link_id,
                        }
                    ]
                }
            )
            [outcome] = self.engine.trade_analyzer.record_execution(
                {
                    "data": [
                        {
                            "category": "spot",
                            "execId": "exit-exec",
                            "symbol": "BTCUSDT",
                            "side": "Sell",
                            "execQty": quantity,
                            "execPrice": "110",
                            "execFee": "0",
                            "feeCurrency": "USDT",
                            "execTime": "1760000001000",
                            "orderLinkId": "",
                        }
                    ]
                }
            )

        self.assertEqual(outcome.signal_ids, tuple(sorted(TECHNICAL_SIGNALS)))
        self.assertEqual(callback.call_args.args[0], outcome)

    def test_live_engine_uses_separate_private_execution_connection(self):
        with (
            patch.dict("os.environ", {"BYBIT_DOMAIN": "bybit.eu"}),
            patch("engine_live.ExecutionStreamListener") as listener_factory,
        ):
            listener = self.engine._start_execution_listener()

        self.assertEqual(listener, listener_factory.return_value)
        kwargs = listener_factory.call_args.kwargs
        self.assertEqual(kwargs["domain"], "bybit")
        self.assertEqual(kwargs["tld"], "eu")
        self.assertTrue(kwargs["testnet"])
        self.assertEqual(kwargs["api_key"], "test-key")
        self.assertEqual(kwargs["api_secret"], "test-secret")
        listener.start.assert_called_once_with()

    def test_live_loop_stops_execution_listener_when_loop_exits(self):
        with (
            patch("engine_live.ExecutionStreamListener") as listener_factory,
            patch.object(self.engine, "_check_daily_drawdown", return_value=False),
        ):
            self.engine.run_live_loop(lambda: ())

        listener_factory.return_value.start.assert_called_once_with()
        listener_factory.return_value.stop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
