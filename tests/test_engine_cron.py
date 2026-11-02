import os
import unittest
from unittest.mock import Mock, patch

from engine_cron import _format_signal_notification, send_ntfy


class SignalNotificationTests(unittest.TestCase):
    def test_formats_buy_signal(self):
        message = _format_signal_notification(
            {
                "action": "BUY",
                "asset": "ASML/EUR",
                "summary": "Strong order book depth supports the signal.",
                "confidence": 0.98,
                "est_profit_sek": 9.4,
            }
        )

        self.assertEqual(
            message,
            "BUY: ASML/EUR\n"
            "WHY: Strong order book depth supports the signal.\n"
            "Win: 98% | Est: 9.4 SEK",
        )

    def test_formats_sell_signal(self):
        message = _format_signal_notification(
            {
                "action": "SELL",
                "asset": "BTC/USDT",
                "pnl": -2.5,
                "pnl_currency": "USDT",
            }
        )

        self.assertEqual(message, "SELL: BTC/USDT\nPnL: -2.5 USDT")

    @patch.dict(os.environ, {"NTFY_TOPIC": "aurora", "NTFY_ACCESS_TOKEN": "secret"})
    @patch("engine_cron.requests.post")
    def test_autonomous_alert_has_no_actions(self, post):
        post.return_value = Mock()

        send_ntfy("BUY: ASML/EUR\nWHY: Reason\nWin: 98% | Est: 9.4 SEK")

        _, request = post.call_args
        self.assertEqual(request["headers"]["Authorization"], "Bearer secret")
        self.assertNotIn("Actions", request["headers"])
        self.assertEqual(
            request["data"],
            b"BUY: ASML/EUR\nWHY: Reason\nWin: 98% | Est: 9.4 SEK",
        )
        post.return_value.raise_for_status.assert_called_once()

    @patch.dict(
        os.environ,
        {
            "NTFY_TOPIC": "aurora",
            "NTFY_APPROVE_URL": "https://example.test/approve",
            "NTFY_CANCEL_URL": "https://example.test/cancel",
        },
        clear=True,
    )
    @patch("engine_cron.requests.post")
    def test_manual_alert_adds_approval_actions(self, post):
        post.return_value = Mock()

        send_ntfy(
            "BUY: BTC/USDT\nWHY: Reason\nWin: 80% | Est: 2 SEK",
            approval_signal={"asset": "BTC/USDT", "allocation": 25},
        )

        actions = post.call_args.kwargs["headers"]["Actions"]
        self.assertIn("http, APPROVE & EXECUTE", actions)
        self.assertIn(
            "https://example.test/approve?decision=approve&asset=BTC%2FUSDT&allocation=25",
            actions,
        )
        self.assertIn("http, CANCEL SIGNAL", actions)
        self.assertIn(
            "https://example.test/cancel?decision=cancel&asset=BTC%2FUSDT&allocation=25",
            actions,
        )

    @patch.dict(os.environ, {"NTFY_TOPIC": "aurora"}, clear=True)
    @patch("engine_cron.requests.post")
    def test_manual_alert_requires_configured_callbacks(self, post):
        with self.assertRaisesRegex(RuntimeError, "NTFY_APPROVE_URL"):
            send_ntfy(
                "BUY: BTC/USDT\nWHY: Reason\nWin: 80% | Est: 2 SEK",
                approval_signal={"asset": "BTC/USDT", "allocation": 25},
            )
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
