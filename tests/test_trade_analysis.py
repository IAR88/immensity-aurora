import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from bybit_execution import ExecutionStreamListener
from trade_analysis import TradeAnalyzer


def execution(
    execution_id,
    side,
    quantity,
    price,
    *,
    fee="0",
    fee_currency="USDT",
    order_link_id="",
):
    return {
        "topic": "execution",
        "data": [
            {
                "category": "spot",
                "execId": execution_id,
                "symbol": "BTCUSDT",
                "side": side,
                "execQty": str(quantity),
                "execPrice": str(price),
                "execFee": fee,
                "feeCurrency": fee_currency,
                "execTime": "1760000000000",
                "orderLinkId": order_link_id,
            }
        ],
    }


class TradeAnalyzerTests(unittest.TestCase):
    def test_completed_trade_is_persisted_notified_and_analyzed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trades.jsonl"
            notifications = []
            analyzer = TradeAnalyzer(path, on_trade_closed=notifications.append)
            analyzer.register_order(
                "entry-order",
                symbol="BTCUSDT",
                signal_ids=("P1", "P2"),
                expected_value_pct=0.02,
            )

            self.assertEqual(
                analyzer.record_execution(
                    execution("buy-fill", "Buy", 1, 100, fee="0.1", order_link_id="entry-order")
                ),
                [],
            )
            outcomes = analyzer.record_execution(
                execution("sell-fill", "Sell", 1, 110, fee="0.1")
            )

            self.assertEqual(len(outcomes), 1)
            outcome = outcomes[0]
            self.assertEqual(outcome.signal_ids, ("P1", "P2"))
            self.assertEqual(outcome.quantity, 1)
            self.assertEqual(outcome.entry_price, 100)
            self.assertEqual(outcome.exit_price, 110)
            self.assertAlmostEqual(outcome.net_pnl, 9.8)
            self.assertIn("first recorded result", outcome.analysis)
            self.assertEqual(notifications, [outcome])

            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["net_pnl"], outcome.net_pnl)
            self.assertEqual(persisted["signal_ids"], ["P1", "P2"])

            reloaded = TradeAnalyzer(path)
            reloaded.register_order(
                "next-order",
                symbol="BTCUSDT",
                signal_ids=("P1",),
                expected_value_pct=0.02,
            )
            reloaded.record_execution(
                execution("next-buy", "Buy", 1, 100, order_link_id="next-order")
            )
            [next_outcome] = reloaded.record_execution(
                execution("next-sell", "Sell", 1, 105)
            )
            self.assertIn("prior 1 trades", next_outcome.analysis)
            self.assertIn("win rate 100.0%", next_outcome.analysis)

    def test_unmatched_sell_is_not_reported_as_a_realized_trade(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trades.jsonl"
            analyzer = TradeAnalyzer(path)
            self.assertEqual(
                analyzer.record_execution(execution("unknown-sell", "Sell", 1, 110)),
                [],
            )
            self.assertFalse(path.exists())

    def test_partial_exit_is_only_completed_after_full_entry_lot_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trades.jsonl"
            analyzer = TradeAnalyzer(path)
            analyzer.register_order(
                "entry-order",
                symbol="BTCUSDT",
                signal_ids=("P1",),
                expected_value_pct=0.02,
            )
            analyzer.record_execution(
                execution("buy-fill", "Buy", 2, 100, order_link_id="entry-order")
            )
            self.assertEqual(
                analyzer.record_execution(execution("partial-exit", "Sell", 1, 110)),
                [],
            )
            analyzer = TradeAnalyzer(path)
            [outcome] = analyzer.record_execution(
                execution("final-exit", "Sell", 1, 120)
            )
            self.assertEqual(outcome.exit_price, 115)
            self.assertAlmostEqual(outcome.net_pnl, 30)

    def test_partial_entry_fills_are_aggregated_into_one_signal_trade(self):
        with tempfile.TemporaryDirectory() as directory:
            analyzer = TradeAnalyzer(Path(directory) / "trades.jsonl")
            analyzer.register_order(
                "entry-order",
                symbol="BTCUSDT",
                signal_ids=("P1",),
                expected_value_pct=0.02,
            )
            analyzer.record_execution(
                execution("buy-first", "Buy", 0.5, 100, order_link_id="entry-order")
            )
            analyzer.record_execution(
                execution("buy-second", "Buy", 0.5, 120, order_link_id="entry-order")
            )
            [outcome] = analyzer.record_execution(
                execution("sell-fill", "Sell", 1, 115)
            )

        self.assertEqual(outcome.entry_price, 110)
        self.assertAlmostEqual(outcome.net_pnl, 5)


class ExecutionStreamListenerTests(unittest.TestCase):
    def test_execution_callback_is_dispatched_off_the_socket_thread(self):
        received = []
        handled = threading.Event()

        class FakeWebSocket:
            def __init__(self, **kwargs):
                self.options = kwargs
                self.callback = None
                self.exited = False

            def execution_stream(self, *, callback):
                self.callback = callback

            def exit(self):
                self.exited = True

        with patch("bybit_execution._create_websocket", FakeWebSocket):
            listener = ExecutionStreamListener(
                testnet=True,
                domain="bybit",
                tld="eu",
                api_key="test-key",
                api_secret="test-secret",
                execution_handler=lambda message: (
                    received.append(message),
                    handled.set(),
                ),
            )
            listener.start()
            websocket = listener._websocket
            websocket.callback({"data": []})
            self.assertTrue(handled.wait(1))
            listener.stop()

        self.assertEqual(received, [{"data": []}])
        self.assertTrue(websocket.exited)
        self.assertEqual(websocket.options["channel_type"], "private")
        self.assertTrue(websocket.options["testnet"])


if __name__ == "__main__":
    unittest.main()
