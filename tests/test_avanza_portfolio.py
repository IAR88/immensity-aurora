import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from avanza_portfolio import (
    JsonResponseCapture,
    _number,
    _requires_login,
    _storage_state_is_usable,
    _write_private_json,
    extract_portfolio,
)


class FakeResponse:
    def __init__(self, url: str, status: int) -> None:
        self.url = url
        self.status = status
        self.headers = {"content-type": "application/json"}

    async def json(self) -> object:
        return {"error": "unauthorized"}


class FakePage:
    def __init__(self, url: str) -> None:
        self.url = url


class AvanzaPortfolioTests(unittest.TestCase):
    def test_extracts_overview_totals_cash_and_stock_holdings(self) -> None:
        payload = {
            "totalPortfolioValue": 12_500.5,
            "cashPositions": [{"balance": "1 500,25"}],
            "withOrderbook": [
                {
                    "account": {"accountName": "ISK"},
                    "orderbook": {
                        "name": "Example AB",
                        "tickerSymbol": "EXMPL",
                        "instrumentType": "STOCK",
                    },
                    "volume": 10,
                    "value": 11_000.25,
                    "currency": "SEK",
                },
                {
                    "orderbook": {
                        "name": "Example Fund",
                        "tickerSymbol": "FUND",
                        "instrumentType": "FUND",
                    },
                    "volume": 2,
                    "value": 50,
                },
            ],
        }

        portfolio = extract_portfolio([payload])

        self.assertIsNotNone(portfolio)
        assert portfolio is not None
        self.assertEqual(portfolio["total_value"], 12_500.5)
        self.assertEqual(portfolio["cash"], 1_500.25)
        self.assertEqual(
            portfolio["holdings"],
            [
                {
                    "name": "Example AB",
                    "symbol": "EXMPL",
                    "quantity": 10.0,
                    "value": 11_000.25,
                    "currency": "SEK",
                    "account": "ISK",
                }
            ],
        )

    def test_does_not_treat_a_holding_value_as_portfolio_total(self) -> None:
        payload = {
            "positions": [
                {
                    "name": "Example AB",
                    "ticker": "EXMPL",
                    "instrumentType": "STOCK",
                    "quantity": 1,
                    "totalValue": 250,
                }
            ]
        }

        portfolio = extract_portfolio([payload])

        self.assertIsNotNone(portfolio)
        assert portfolio is not None
        self.assertIsNone(portfolio["total_value"])
        self.assertEqual(portfolio["holdings"][0]["value"], 250.0)

    def test_parses_swedish_and_international_number_formats(self) -> None:
        self.assertEqual(_number("1 234,56"), 1_234.56)
        self.assertEqual(_number("1,234.56"), 1_234.56)

    def test_expired_api_response_is_detected(self) -> None:
        capture = JsonResponseCapture(RuntimeError)
        response = FakeResponse(
            "https://www.avanza.se/api/account/overview", status=401
        )

        asyncio.run(capture._capture(response))

        self.assertTrue(capture.session_expired)

    def test_login_page_detection(self) -> None:
        self.assertFalse(
            _requires_login(FakePage("https://www.avanza.se/overview/overview.html"))
        )
        self.assertTrue(_requires_login(FakePage("https://www.avanza.se/login")))

    def test_session_state_and_private_json_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "avanza_session.json"
            self.assertFalse(_storage_state_is_usable(state_file))
            _write_private_json(
                state_file, {"cookies": [], "origins": [{"origin": "https://avanza.se"}]}
            )
            self.assertTrue(_storage_state_is_usable(state_file))
            self.assertEqual(
                state_file.stat().st_mode & 0o777,
                0o600,
            )
            self.assertEqual(
                json.loads(state_file.read_text(encoding="utf-8"))["cookies"], []
            )

    def test_returns_none_for_unrecognized_json(self) -> None:
        self.assertIsNone(extract_portfolio([{"message": "not portfolio data"}]))


if __name__ == "__main__":
    unittest.main()
