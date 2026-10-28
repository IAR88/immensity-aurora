import unittest
from unittest.mock import Mock, patch

from engine.opportunity_optimizer import OpportunityCostOptimizer
from engine_live import HjarnanLiveEngine


class OpportunityCostOptimizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.optimizer = OpportunityCostOptimizer()
        self.active_position = {
            "symbol": "BTCUSDT",
            "entry_time": 1_000.0,
            "current_roi": 0.01,
        }
        self.new_signal = {"symbol": "ETHUSDT", "new_roi": 0.02}

    def test_defaults(self) -> None:
        self.assertEqual(self.optimizer.fee_hurdle, 0.0012)
        self.assertEqual(self.optimizer.safety_margin, 0.005)
        self.assertEqual(self.optimizer.cooldown_seconds, 60.0)

    def test_rejects_reallocation_during_cooldown(self) -> None:
        with patch("engine.opportunity_optimizer.time.time", return_value=1_059.99):
            self.assertFalse(
                self.optimizer.evaluate_reallocation(
                    {"entry_time": 1_000.0}, {"symbol": "ETHUSDT"}
                )
            )

    def test_cooldown_boundary_allows_roi_evaluation(self) -> None:
        self.new_signal["new_roi"] = 0.02
        with patch("engine.opportunity_optimizer.time.time", return_value=1_060.0):
            self.assertTrue(
                self.optimizer.evaluate_reallocation(
                    self.active_position, self.new_signal
                )
            )

    def test_requires_advantage_strictly_above_cost_hurdle_and_margin(self) -> None:
        self.active_position["current_roi"] = 0.0
        self.new_signal["new_roi"] = (
            self.optimizer.fee_hurdle + self.optimizer.safety_margin
        )
        with patch("engine.opportunity_optimizer.time.time", return_value=1_060.0):
            self.assertFalse(
                self.optimizer.evaluate_reallocation(
                    self.active_position, self.new_signal
                )
            )
            self.new_signal["new_roi"] += 0.000001
            self.assertTrue(
                self.optimizer.evaluate_reallocation(
                    self.active_position, self.new_signal
                )
            )

    def test_rejects_missing_or_non_finite_inputs(self) -> None:
        with patch("engine.opportunity_optimizer.time.time", return_value=1_060.0):
            with self.assertRaisesRegex(ValueError, "entry_time"):
                self.optimizer.evaluate_reallocation(
                    {"current_roi": 0.0}, self.new_signal
                )
            with self.assertRaisesRegex(ValueError, "new_roi"):
                self.optimizer.evaluate_reallocation(
                    self.active_position, {"symbol": "ETHUSDT"}
                )

    def test_rejects_negative_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "cooldown_seconds"):
            OpportunityCostOptimizer(cooldown_seconds=-1)
        with self.assertRaisesRegex(ValueError, "fee_hurdle"):
            OpportunityCostOptimizer(fee_hurdle=-0.1)

    def test_process_market_cycle_closes_then_enters_best_different_symbol(self) -> None:
        engine = HjarnanLiveEngine.__new__(HjarnanLiveEngine)
        engine.opportunity_optimizer = OpportunityCostOptimizer()
        position = {
            "symbol": "BTCUSDT",
            "entry_time": 1_000.0,
            "current_roi": 0.0,
        }
        same_symbol_signal = {"symbol": "BTCUSDT", "new_roi": 0.9}
        weaker_signal = {"symbol": "ETHUSDT", "new_roi": 0.01}
        better_signal = {"symbol": "SOLUSDT", "new_roi": 0.02}
        events: list[tuple[str, str]] = []

        with patch("engine.opportunity_optimizer.time.time", return_value=1_060.0):
            reallocations = engine.process_market_cycle(
                [position],
                [same_symbol_signal, weaker_signal, better_signal],
                liquidate_position=lambda active: events.append(
                    ("close", str(active["symbol"]))
                ),
                enter_signal=lambda signal: events.append(
                    ("enter", str(signal["symbol"]))
                ),
            )

        self.assertEqual(reallocations, 1)
        self.assertEqual(events, [("close", "BTCUSDT"), ("enter", "SOLUSDT")])

    def test_process_market_cycle_does_not_enter_after_liquidation_failure(self) -> None:
        engine = HjarnanLiveEngine.__new__(HjarnanLiveEngine)
        engine.opportunity_optimizer = OpportunityCostOptimizer()
        enter_signal = Mock()
        with patch("engine.opportunity_optimizer.time.time", return_value=1_060.0):
            with self.assertRaisesRegex(RuntimeError, "Liquidation failed"):
                engine.process_market_cycle(
                    [self.active_position],
                    [self.new_signal],
                    liquidate_position=lambda _: False,
                    enter_signal=enter_signal,
                )
        enter_signal.assert_not_called()

    def test_process_market_cycle_uses_engine_bybit_callbacks_by_default(self) -> None:
        engine = HjarnanLiveEngine.__new__(HjarnanLiveEngine)
        engine.opportunity_optimizer = OpportunityCostOptimizer()
        engine._liquidate_bybit_position = Mock(return_value={"retCode": 0})
        engine._enter_bybit_signal = Mock(return_value={"retCode": 0})
        with patch("engine.opportunity_optimizer.time.time", return_value=1_060.0):
            count = engine.process_market_cycle(
                [self.active_position], [self.new_signal]
            )
        self.assertEqual(count, 1)
        engine._liquidate_bybit_position.assert_called_once_with(
            self.active_position
        )
        engine._enter_bybit_signal.assert_called_once_with(self.new_signal)

    def test_engine_initializes_optimizer(self) -> None:
        sessions = Mock()
        sessions.get_bybit_session.return_value = Mock(testnet=True)
        engine = HjarnanLiveEngine(sessions)
        self.assertIsInstance(engine.opportunity_optimizer, OpportunityCostOptimizer)
