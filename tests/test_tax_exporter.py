import csv
import importlib.util
import sys
import tempfile
import types
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from tax_exporter import (
    CSV_FIELDS,
    DAC8_CSV_FIELDS,
    DAC8_EXPORT_LABEL,
    build_dac8_rows,
    build_k4_rows,
    fetch_avanza_history,
    fetch_bybit_history,
    parse_timestamp,
    write_dac8_csv,
    write_dac8_json,
    write_k4_csv,
    write_k4_pdf,
)


class TaxExporterTests(unittest.TestCase):
    def test_bybit_fifo_matches_oldest_acquisition_first(self) -> None:
        rows = build_k4_rows(
            [
                {
                    "execId": "buy-old",
                    "execTime": "1735689600000",
                    "symbol": "BTCUSDT",
                    "side": "Buy",
                    "execQty": "1",
                    "execPrice": "100",
                    "execValue": "100",
                    "quoteCoin": "USDT",
                    "fx_rate_sek": "10",
                },
                {
                    "execId": "buy-new",
                    "execTime": "1735776000000",
                    "symbol": "BTCUSDT",
                    "side": "Buy",
                    "execQty": "1",
                    "execPrice": "200",
                    "execValue": "200",
                    "quoteCoin": "USDT",
                    "fx_rate_sek": "10",
                },
                {
                    "execId": "sell",
                    "execTime": "1735862400000",
                    "symbol": "BTCUSDT",
                    "side": "Sell",
                    "execQty": "1.5",
                    "execPrice": "300",
                    "execValue": "450",
                    "quoteCoin": "USDT",
                    "fx_rate_sek": "10",
                },
            ],
            [],
        )

        self.assertEqual(rows[0]["acquisition_cost_sek"], "2000.00")
        self.assertEqual(rows[0]["total_sek"], "4500.00")
        self.assertEqual(rows[0]["pnl_sek"], "2500.00")

    def test_avanza_average_cost_and_schablon_method(self) -> None:
        history = [
            {
                "id": "buy-1",
                "type": "BUY",
                "timestamp": "2025-01-01",
                "instrument": "SE0000000001",
                "quantity": "10",
                "price_native": "100",
                "currency": "SEK",
            },
            {
                "id": "buy-2",
                "type": "BUY",
                "timestamp": "2025-01-02",
                "instrument": "SE0000000001",
                "quantity": "10",
                "price_native": "200",
                "currency": "SEK",
            },
            {
                "id": "sell",
                "type": "SELL",
                "timestamp": "2025-01-03",
                "instrument": "SE0000000001",
                "quantity": "10",
                "price_native": "250",
                "currency": "SEK",
            },
        ]

        average_row = build_k4_rows([], history)[0]
        schablon_row = build_k4_rows(
            [], history, avanza_cost_method="schablon"
        )[0]
        self.assertEqual(average_row["acquisition_cost_sek"], "1500.00")
        self.assertEqual(average_row["pnl_sek"], "1000.00")
        self.assertEqual(schablon_row["acquisition_cost_sek"], "500.00")
        self.assertEqual(schablon_row["pnl_sek"], "2000.00")

    def test_avanza_api_transaction_model_and_sek_commission(self) -> None:
        common = {
            "orderbook": {"currency": "USD", "isin": "US0000000001"},
            "volume": {"value": 2, "unit": "units"},
            "priceInTradedCurrency": {"value": 100, "unit": "USD"},
            "currencyRate": {"value": 10, "unit": "SEK"},
            "commission": {"value": 1, "unit": "SEK"},
        }
        history = [
            {
                **common,
                "id": "buy",
                "date": "2025-01-01T10:00:00",
                "type": "BUY",
            },
            {
                **common,
                "id": "sell",
                "date": "2025-01-02T10:00:00",
                "type": "SELL",
                "priceInTradedCurrency": {"value": 120, "unit": "USD"},
            },
        ]

        row = build_k4_rows([], history)[0]
        self.assertEqual(row["instrument"], "US0000000001")
        self.assertEqual(row["fx_rate_sek"], "10.00")
        self.assertEqual(row["total_sek"], "2399.00")
        self.assertEqual(row["acquisition_cost_sek"], "2001.00")
        self.assertEqual(row["pnl_sek"], "398.00")

    def test_base_currency_bybit_fee_adjusts_received_quantity_and_cost(self) -> None:
        rows = build_k4_rows(
            [
                {
                    "execId": "buy",
                    "execTime": "1735689600000",
                    "symbol": "BTCUSDT",
                    "side": "Buy",
                    "execQty": "1",
                    "execPrice": "100",
                    "execValue": "100",
                    "execFee": "0.01",
                    "feeCurrency": "BTC",
                    "quoteCoin": "USDT",
                    "fx_rate_sek": "10",
                },
                {
                    "execId": "sell",
                    "execTime": "1735776000000",
                    "symbol": "BTCUSDT",
                    "side": "Sell",
                    "execQty": "0.99",
                    "execPrice": "200",
                    "execValue": "198",
                    "quoteCoin": "USDT",
                    "fx_rate_sek": "10",
                },
            ],
            [],
        )
        self.assertEqual(rows[0]["acquisition_cost_sek"], "1010.00")
        self.assertEqual(rows[0]["pnl_sek"], "970.00")

    def test_timestamp_parser_handles_iso_and_millisecond_values(self) -> None:
        iso = parse_timestamp("2025-01-01T01:00:00+01:00")
        epoch_ms = parse_timestamp(1735689600000)
        self.assertEqual(iso, epoch_ms)

    def test_avanza_naive_timestamp_is_interpreted_as_swedish_local_time(self) -> None:
        history = [
            {
                "id": "buy",
                "type": "BUY",
                "date": "2025-01-01T00:00:00",
                "isin": "SE0000000001",
                "orderbook": {"currency": "SEK"},
                "volume": {"value": 1},
                "priceInTradedCurrency": {"value": 100},
            },
            {
                "id": "sell",
                "type": "SELL",
                "date": "2025-01-02T00:00:00",
                "isin": "SE0000000001",
                "orderbook": {"currency": "SEK"},
                "volume": {"value": 1},
                "priceInTradedCurrency": {"value": 110},
            },
        ]
        row = build_k4_rows([], history)[0]
        self.assertEqual(row["timestamp"], "2025-01-02T00:00:00+01:00")

    def test_bybit_history_follows_pagination_cursor(self) -> None:
        class MockBybit:
            def __init__(self) -> None:
                self.cursors: list[str | None] = []

            def get_executions(self, **params: object) -> dict[str, object]:
                cursor = params.get("cursor")
                self.cursors.append(str(cursor) if cursor else None)
                if cursor is None:
                    return {
                        "retCode": 0,
                        "result": {
                            "list": [{"execId": "one"}],
                            "nextPageCursor": "next",
                        },
                    }
                return {
                    "retCode": 0,
                    "result": {"list": [{"execId": "two"}], "nextPageCursor": ""},
                }

        session = MockBybit()
        records = fetch_bybit_history(
            session, date(2025, 1, 1), date(2025, 1, 1)
        )
        self.assertEqual([row["execId"] for row in records], ["one", "two"])
        self.assertEqual(session.cursors, [None, "next"])

    def test_avanza_history_queries_buys_and_sells(self) -> None:
        transaction_types = types.ModuleType("avanza.constants")
        transaction_types.TransactionsDetailsType = types.SimpleNamespace(
            BUY="BUY",
            SELL="SELL",
        )
        source_rows = [
            {
                "id": "buy",
                "type": "BUY_SELL",
                "date": "2025-01-01T10:00:00",
                "isin": "SE0000000001",
                "orderbook": {"currency": "SEK"},
                "volume": {"value": 1},
                "priceInTradedCurrency": {"value": 100},
                "side": "BUY",
            },
            {
                "id": "sell",
                "type": "BUY_SELL",
                "date": "2025-01-02T10:00:00",
                "isin": "SE0000000001",
                "orderbook": {"currency": "SEK"},
                "volume": {"value": 1},
                "priceInTradedCurrency": {"value": 110},
                "side": "SELL",
            },
        ]

        class MockAvanza:
            def __init__(self) -> None:
                self.queried_sides: list[str] = []

            def get_transactions_details(
                self, **kwargs: object
            ) -> dict[str, object]:
                side = str(kwargs["transaction_details_types"][0])
                self.queried_sides.append(side)
                return {
                    "transactions": [
                        {
                            key: value
                            for key, value in row.items()
                            if key != "side"
                        }
                        for row in source_rows
                        if row["side"] == side
                    ]
                }

        session = MockAvanza()
        with patch.dict(sys.modules, {"avanza.constants": transaction_types}):
            records = fetch_avanza_history(
                session, date(2025, 1, 1), date(2025, 1, 2)
            )

        rows = build_k4_rows([], records)
        self.assertEqual(session.queried_sides, ["BUY", "SELL"])
        self.assertEqual(rows[0]["pnl_sek"], "10.00")

    def test_csv_has_k4_columns_and_expected_values(self) -> None:
        rows = build_k4_rows(
            [
                {
                    "execId": "buy",
                    "execTime": "1735689600000",
                    "symbol": "BTCUSDT",
                    "side": "Buy",
                    "execQty": "1",
                    "execPrice": "100",
                    "execValue": "100",
                    "quoteCoin": "USDT",
                    "fx_rate_sek": "10",
                },
                {
                    "execId": "sell",
                    "execTime": "1735776000000",
                    "symbol": "BTCUSDT",
                    "side": "Sell",
                    "execQty": "1",
                    "execPrice": "110",
                    "execValue": "110",
                    "quoteCoin": "USDT",
                    "fx_rate_sek": "10",
                },
            ],
            [],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = write_k4_csv(rows, Path(directory) / "K4.csv")
            with path.open(newline="", encoding="utf-8-sig") as csv_file:
                reader = csv.DictReader(csv_file)
                self.assertEqual(tuple(reader.fieldnames or ()), CSV_FIELDS)
                saved_rows = list(reader)
        self.assertEqual(saved_rows[0]["pnl_sek"], "100.00")

    def test_dac8_ledger_exports_required_fields_and_utc_timestamps(self) -> None:
        k4_rows = build_k4_rows(
            [
                {
                    "execId": "crypto-sale",
                    "execTime": "1735862400000",
                    "symbol": "BTCUSDT",
                    "side": "Sell",
                    "execQty": "1",
                    "execPrice": "300",
                    "execValue": "300",
                    "quoteCoin": "USDT",
                    "fx_rate_sek": "10",
                },
                {
                    "execId": "crypto-buy",
                    "execTime": "1735689600000",
                    "symbol": "BTCUSDT",
                    "side": "Buy",
                    "execQty": "1",
                    "execPrice": "100",
                    "execValue": "100",
                    "quoteCoin": "USDT",
                    "fx_rate_sek": "10",
                },
            ],
            [],
        )
        ledger = build_dac8_rows(k4_rows)
        self.assertEqual(ledger[0]["export_label"], DAC8_EXPORT_LABEL)
        self.assertEqual(ledger[0]["timestamp_utc"], "2025-01-03T00:00:00Z")
        self.assertEqual(ledger[0]["asset_type"], "CRYPTO")
        self.assertEqual(ledger[0]["transaction_hash_or_id"], "crypto-sale")
        self.assertEqual(ledger[0]["counterparty_type"], "exchange")
        with tempfile.TemporaryDirectory() as directory:
            csv_path = write_dac8_csv(ledger, Path(directory) / "dac8.csv")
            json_path = write_dac8_json(ledger, Path(directory) / "dac8.json")
            with csv_path.open(newline="", encoding="utf-8-sig") as csv_file:
                reader = csv.DictReader(csv_file)
                self.assertEqual(tuple(reader.fieldnames or ()), DAC8_CSV_FIELDS)
                self.assertEqual(next(reader)["gross_proceeds_sek"], "3000.00")
            self.assertIn(DAC8_EXPORT_LABEL, json_path.read_text(encoding="utf-8"))

            if not all(
                importlib.util.find_spec(name) for name in ("reportlab", "PIL")
            ):
                self.skipTest("K4 PDF export requires reportlab and Pillow.")

            pdf_path = write_k4_pdf(k4_rows, Path(directory) / "K4.pdf")
            self.assertTrue(pdf_path.read_bytes().startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
