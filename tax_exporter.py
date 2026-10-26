from __future__ import annotations

import argparse
import csv
import json
import logging
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from auth.sessions_manager import SessionsManager

logger = logging.getLogger(__name__)

CSV_FIELDS = (
    "transaction_id",
    "timestamp",
    "instrument",
    "type",
    "quantity",
    "price_native",
    "currency",
    "fx_rate_sek",
    "total_sek",
    "acquisition_cost_sek",
    "pnl_sek",
)
DAC8_EXPORT_LABEL = "DAC8/CARF Transaction Ledger Export"
DAC8_CSV_FIELDS = (
    "export_label",
    "timestamp_utc",
    "asset_type",
    "transaction_hash_or_id",
    "gross_proceeds_sek",
    "acquisition_cost_sek",
    "realized_gain_loss_sek",
    "counterparty_type",
)
MAX_AVANZA_TRANSACTIONS = 1000
BYBIT_WINDOW = timedelta(days=7)
_CENT = Decimal("0.01")

FxRateProvider = Callable[[str, datetime], Decimal | int | float | str]
AvanzaCostMethod = Literal["average", "schablon"]


@dataclass(frozen=True, slots=True)
class _Trade:
    transaction_id: str
    timestamp: datetime
    instrument: str
    side: Literal["BUY", "SELL"]
    quantity: Decimal
    price_native: Decimal
    currency: str
    fx_rate_sek: Decimal
    gross_sek: Decimal
    fee_sek: Decimal
    counterparty_type: str


@dataclass(slots=True)
class _Lot:
    quantity: Decimal
    cost_sek: Decimal


def parse_timestamp(
    value: object,
    *,
    default_timezone: tzinfo = timezone.utc,
    normalize_utc: bool = True,
) -> datetime:
    """Parse an ISO timestamp, date, or Unix timestamp (seconds or milliseconds)."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, (int, float, Decimal)):
        numeric = Decimal(str(value))
        if not numeric.is_finite():
            raise ValueError(f"Invalid transaction timestamp: {value!r}")
        if abs(numeric) >= Decimal("100000000000"):
            numeric /= 1000
        parsed = datetime.fromtimestamp(float(numeric), tz=timezone.utc)
    elif isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            raise ValueError("Transaction timestamp cannot be empty.")
        try:
            numeric = Decimal(normalized)
        except InvalidOperation:
            numeric = None
        if numeric is not None:
            return parse_timestamp(
                numeric, default_timezone=default_timezone, normalize_utc=normalize_utc
            )
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as error:
            raise ValueError(
                f"Unsupported transaction timestamp: {value!r}"
            ) from error
    else:
        raise TypeError(
            f"Unsupported transaction timestamp type: {type(value).__name__}"
        )

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=default_timezone)
    return parsed.astimezone(timezone.utc) if normalize_utc else parsed


def _decimal(value: object, field: str, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"{field} must be a valid number, got {value!r}.") from error
    if not result.is_finite() or (positive and result <= 0):
        qualifier = "positive and finite" if positive else "finite"
        raise ValueError(f"{field} must be {qualifier}, got {value!r}.")
    return result


def _field(record: Mapping[str, object], *names: str) -> object | None:
    for name in names:
        value = record.get(name)
        if value is not None and value != "":
            return value
    return None


def _nested(record: Mapping[str, object], *names: str) -> Mapping[str, object]:
    for name in names:
        value = record.get(name)
        if isinstance(value, Mapping):
            return value
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump()
            if isinstance(dumped, Mapping):
                return dumped
    return {}


def _info_value(value: object) -> object | None:
    if isinstance(value, Mapping):
        return _field(value, "value", "amount")
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, Mapping):
            return _field(dumped, "value", "amount")
    if value is not None:
        return value
    return None


def _info_unit(value: object) -> str | None:
    if isinstance(value, Mapping):
        unit = _field(value, "unit", "currency")
    else:
        model_dump = getattr(value, "model_dump", None)
        if not callable(model_dump):
            return None
        dumped = model_dump()
        unit = (
            _field(dumped, "unit", "currency")
            if isinstance(dumped, Mapping)
            else None
        )
    return str(unit).upper() if unit is not None else None


def _records(payload: object, *keys: str) -> list[Mapping[str, object]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, Mapping)]
    model_dump = getattr(payload, "model_dump", None)
    if callable(model_dump):
        payload = model_dump()
    if not isinstance(payload, Mapping):
        raise TypeError("Exchange history response must be a mapping or list.")
    for key in keys:
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, Mapping)]
        if isinstance(rows, Mapping):
            try:
                return _records(rows, *keys)
            except ValueError:
                continue
    for key in ("data", "result", "content"):
        nested = payload.get(key)
        if isinstance(nested, (Mapping, list)):
            try:
                return _records(nested, *keys)
            except ValueError:
                continue
    if any(key in payload for key in keys):
        return []
    raise ValueError(
        f"Exchange response did not contain any recognized history list: {keys!r}."
    )


def _check_bybit_response(payload: object) -> Mapping[str, object]:
    if not isinstance(payload, Mapping):
        raise TypeError("Bybit response must be a mapping.")
    ret_code = payload.get("retCode")
    if ret_code is not None and str(ret_code) != "0":
        raise RuntimeError(
            f"Bybit history request failed ({ret_code}): {payload.get('retMsg', '')}"
        )
    return payload


def fetch_bybit_history(
    session: object, start_date: date, end_date: date
) -> list[Mapping[str, object]]:
    """Fetch Bybit spot executions in seven-day windows, following every cursor."""
    get_executions = getattr(session, "get_executions", None)
    if not callable(get_executions):
        raise RuntimeError("Bybit session does not provide get_executions().")
    if start_date > end_date:
        raise ValueError("History start date must be on or before end date.")

    start = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc)
    executions: dict[str, Mapping[str, object]] = {}

    window_start = start
    while window_start < end:
        window_end = min(window_start + BYBIT_WINDOW, end)
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            params: dict[str, object] = {
                "category": "spot",
                "startTime": int(window_start.timestamp() * 1000),
                "endTime": int(window_end.timestamp() * 1000),
                "limit": 100,
            }
            if cursor:
                params["cursor"] = cursor
            payload = _check_bybit_response(get_executions(**params))
            result = payload.get("result")
            if not isinstance(result, Mapping):
                raise ValueError("Bybit response did not contain a result mapping.")
            rows = _records(result, "list")
            for index, row in enumerate(rows):
                execution_id = _field(row, "execId", "transaction_id", "id")
                if execution_id is None:
                    execution_id = (
                        f"{row.get('symbol', '')}:{row.get('execTime', '')}:"
                        f"{row.get('execQty', '')}:{row.get('execPrice', '')}:{index}"
                    )
                executions[str(execution_id)] = row

            next_cursor = (
                result.get("nextPageCursor")
                if isinstance(result, Mapping)
                else None
            )
            if not next_cursor:
                break
            cursor = str(next_cursor)
            if cursor in seen_cursors:
                raise RuntimeError(
                    "Bybit returned a repeated execution-history cursor."
                )
            seen_cursors.add(cursor)
        window_start = window_end

    return list(executions.values())


def _extract_avanza_rows(payload: object) -> list[Mapping[str, object]]:
    return _records(
        payload,
        "transactions",
        "transactionDetails",
        "items",
        "rows",
        "list",
    )


def fetch_avanza_history(
    session: object, start_date: date, end_date: date
) -> list[Mapping[str, object]]:
    """Fetch all Avanza transactions, splitting full pages to avoid truncation."""
    get_transactions = getattr(session, "get_transactions_details", None)
    if not callable(get_transactions):
        raise RuntimeError(
            "Avanza session does not provide get_transactions_details(); "
            "install the avanza-api client and authenticate first."
        )
    try:
        from avanza.constants import TransactionsDetailsType
    except ImportError as error:
        raise RuntimeError(
            "Avanza history export requires the avanza-api "
            "TransactionsDetailsType definitions."
        ) from error
    if start_date > end_date:
        raise ValueError("History start date must be on or before end date.")

    collected: dict[str, Mapping[str, object]] = {}

    def fetch_window(
        first: date, last: date, side: Literal["BUY", "SELL"]
    ) -> None:
        transaction_type = (
            TransactionsDetailsType.BUY
            if side == "BUY"
            else TransactionsDetailsType.SELL
        )
        payload = get_transactions(
            transaction_details_types=[transaction_type],
            transactions_from=first,
            transactions_to=last,
            max_elements=MAX_AVANZA_TRANSACTIONS,
        )
        rows = _extract_avanza_rows(payload)
        if len(rows) >= MAX_AVANZA_TRANSACTIONS:
            if first >= last:
                raise RuntimeError(
                    f"Avanza returned the {MAX_AVANZA_TRANSACTIONS}-transaction "
                    f"limit on {first}; history cannot be guaranteed complete."
                )
            midpoint = first + (last - first) // 2
            fetch_window(first, midpoint, side)
            fetch_window(midpoint + timedelta(days=1), last, side)
            return

        for index, row in enumerate(rows):
            normalized_row = dict(row)
            normalized_row["_tax_export_side"] = side
            identifier = _field(row, "transaction_id", "transactionId", "id")
            if identifier is None:
                identifier = (
                    f"{_field(row, 'transactionDate', 'date', 'timestamp')}:"
                    f"{_field(row, 'instrument', 'name', 'orderbookId')}:"
                    f"{_field(row, 'type', 'transactionType')}:{index}"
                )
            collected[str(identifier)] = normalized_row

    fetch_window(start_date, end_date, "BUY")
    fetch_window(start_date, end_date, "SELL")
    return list(collected.values())


def _side(value: object) -> Literal["BUY", "SELL"] | None:
    if value is None:
        return None
    normalized = str(value).strip().upper().replace("-", "_").replace(" ", "_")
    if normalized in {"BUY", "B", "PURCHASE", "BOUGHT"} or normalized.endswith("_BUY"):
        return "BUY"
    if normalized in {"SELL", "S", "SALE", "SOLD"} or normalized.endswith("_SELL"):
        return "SELL"
    return None


def _resolve_fx_rate(
    record: Mapping[str, object],
    currency: str,
    timestamp: datetime,
    fx_rate_provider: FxRateProvider | None,
) -> Decimal:
    if currency == "SEK":
        return Decimal("1")
    explicit_rate = _field(record, "fx_rate_sek", "fxRateSek", "exchangeRate")
    if explicit_rate is None:
        explicit_rate = _info_value(record.get("currencyRate"))
    if explicit_rate is not None:
        return _decimal(explicit_rate, "fx_rate_sek", positive=True)
    if fx_rate_provider is None:
        raise ValueError(
            f"Missing SEK conversion rate for {currency} at "
            f"{timestamp.isoformat()}; provide fx_rate_sek or an FX-rate provider."
        )
    return _decimal(
        fx_rate_provider(currency, timestamp),
        f"{currency}/SEK rate",
        positive=True,
    )


def _parse_bybit_trade(
    record: Mapping[str, object], fx_rate_provider: FxRateProvider | None
) -> _Trade:
    if str(record.get("execType", "Trade")).lower() != "trade":
        raise ValueError("Only Bybit trade executions can be exported.")
    timestamp = parse_timestamp(
        _field(record, "timestamp", "execTime", "time", "tradeTime")
    )
    symbol = str(_field(record, "symbol", "instrument") or "").strip().upper()
    if not symbol:
        raise ValueError("Bybit execution is missing its symbol.")
    currency = str(
        _field(record, "currency", "quoteCoin", "settlementCurrency")
        or _infer_quote_currency(symbol)
    ).upper()
    instrument = str(
        _field(record, "instrument", "baseCoin") or _infer_base_currency(symbol)
    )
    side = _side(_field(record, "side", "type"))
    if side is None:
        raise ValueError(f"Bybit execution has an unknown side: {record!r}")

    quantity = _decimal(
        _field(record, "quantity", "execQty", "qty"), "quantity", positive=True
    )
    price = _decimal(
        _field(record, "price_native", "execPrice", "price"),
        "price_native",
        positive=True,
    )
    rate = _resolve_fx_rate(record, currency, timestamp, fx_rate_provider)
    raw_total_sek = _field(record, "total_sek", "totalSek")
    gross_sek = (
        _decimal(raw_total_sek, "total_sek", positive=True)
        if raw_total_sek is not None
        else _decimal(
            _field(record, "execValue", "value") or quantity * price,
            "Bybit execution value",
            positive=True,
        )
        * rate
    )
    fee = _decimal(_field(record, "fee", "execFee") or 0, "fee")
    fee_currency = str(
        _field(record, "feeCurrency", "fee_currency") or currency
    ).upper()
    if fee_currency == currency:
        fee_sek = abs(fee) * rate
    elif fee_currency == instrument.upper():
        if side == "SELL" and fee != 0:
            raise ValueError(
                f"Bybit charged a sell fee in the disposed base asset for "
                f"{record.get('execId', symbol)}; that fee is a separate "
                "crypto disposal and is not supported by this export."
            )
        fee_sek = abs(fee) * price * rate
    elif fee == 0:
        fee_sek = Decimal("0")
    else:
        raise ValueError(
            f"Bybit charged a fee in {fee_currency} for "
            f"{record.get('execId', symbol)}; "
            "fees paid in a third crypto asset require a separate disposal record."
        )

    position_quantity = quantity
    if fee_currency == instrument.upper():
        if side == "BUY":
            position_quantity -= abs(fee)
        else:
            position_quantity += abs(fee)
        if position_quantity <= 0:
            raise ValueError(
                f"Bybit base-asset fee consumes the full execution quantity: "
                f"{record!r}"
            )

    return _Trade(
        transaction_id=str(_field(record, "transaction_id", "execId", "id") or ""),
        timestamp=timestamp,
        instrument=instrument,
        side=side,
        quantity=position_quantity,
        price_native=price,
        currency=currency,
        fx_rate_sek=rate,
        gross_sek=gross_sek,
        fee_sek=fee_sek,
        counterparty_type=str(_field(record, "counterparty_type", "counterpartyType") or "exchange"),
    )


def _infer_quote_currency(symbol: str) -> str:
    known_quotes = ("USDT", "USDC", "BUSD", "EUR", "BTC", "ETH", "USD", "SEK")
    for quote in known_quotes:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return quote
    raise ValueError(
        f"Cannot infer quote currency from Bybit symbol {symbol!r}; "
        "provide quoteCoin or currency in the execution record."
    )


def _infer_base_currency(symbol: str) -> str:
    quote = _infer_quote_currency(symbol)
    return symbol[: -len(quote)]


def _parse_avanza_trade(
    record: Mapping[str, object], fx_rate_provider: FxRateProvider | None
) -> _Trade | None:
    details = _nested(record, "transactionDetails", "details")
    side_value = (
        _field(record, "_tax_export_side")
        or _field(details, "type", "transactionType", "side")
        or _field(record, "type", "transactionType", "side")
    )
    side = _side(side_value)
    transaction_kind = str(
        _field(record, "transactionType", "type") or ""
    ).upper()
    if side is None:
        if transaction_kind in {"BUY_SELL", "BUY-SELL"}:
            raise ValueError(
                f"Avanza buy/sell transaction has an unknown side: {record!r}"
            )
        return None

    orderbook = _nested(record, "orderbook", "instrumentDetails")
    instrument = _field(
        record,
        "instrument",
        "isin",
        "tickerSymbol",
        "instrumentName",
        "orderbookId",
        "symbol",
        "name",
    ) or _field(orderbook, "isin", "tickerSymbol", "id", "name", "symbol")
    if instrument is None:
        raise ValueError("Avanza transaction is missing an instrument identifier.")
    timestamp = parse_timestamp(
        _field(
            record,
            "timestamp",
            "transactionDate",
            "date",
            "tradeDate",
            "bookingDate",
        ),
        default_timezone=ZoneInfo("Europe/Stockholm"),
        normalize_utc=False,
    )
    quantity = _decimal(
        _field(record, "quantity", "qty")
        or _info_value(record.get("volume"))
        or _field(details, "quantity", "volume", "qty"),
        "quantity",
        positive=True,
    )
    price = _decimal(
        _field(record, "price_native", "price")
        or _info_value(record.get("priceInTradedCurrency"))
        or _info_value(record.get("priceInTransactionCurrency"))
        or _field(details, "price", "pricePerShare"),
        "price_native",
        positive=True,
    )
    currency_value = _field(record, "currency", "tradeCurrency") or _field(
        orderbook, "currency"
    )
    if currency_value is None:
        raise ValueError(
            f"Avanza transaction for {instrument!r} is missing its currency."
        )
    currency = str(currency_value).upper()
    rate = _resolve_fx_rate(record, currency, timestamp, fx_rate_provider)
    raw_total_sek = _field(record, "total_sek", "totalSek")
    gross_sek = (
        _decimal(raw_total_sek, "total_sek", positive=True)
        if raw_total_sek is not None
        else quantity * price * rate
    )
    commission_value = _field(
        record, "fee", "brokerageFee", "transactionFee"
    ) or _info_value(record.get("commission"))
    fee = abs(_decimal(commission_value or 0, "fee"))
    fee_currency = str(
        _field(record, "feeCurrency", "fee_currency")
        or _info_unit(record.get("commission"))
        or currency
    ).upper()
    fee_rate = (
        rate
        if fee_currency == currency
        else _resolve_fx_rate({}, fee_currency, timestamp, fx_rate_provider)
    )
    fee_sek = fee * fee_rate
    transaction_id = _field(
        record, "transaction_id", "transactionId", "id", "orderId"
    )
    return _Trade(
        transaction_id=str(transaction_id or ""),
        timestamp=timestamp,
        instrument=str(instrument),
        side=side,
        quantity=quantity,
        price_native=price,
        currency=currency,
        fx_rate_sek=rate,
        gross_sek=gross_sek,
        fee_sek=fee_sek,
        counterparty_type=str(_field(record, "counterparty_type", "counterpartyType") or "broker"),
    )


def build_k4_rows(
    bybit_history: Iterable[Mapping[str, object]],
    avanza_history: Iterable[Mapping[str, object]],
    *,
    fx_rate_provider: FxRateProvider | None = None,
    avanza_cost_method: AvanzaCostMethod = "average",
    report_start: date | None = None,
) -> list[dict[str, str]]:
    """Calculate realized disposals using Bybit FIFO and Avanza average cost."""
    if avanza_cost_method not in {"average", "schablon"}:
        raise ValueError("Avanza cost method must be 'average' or 'schablon'.")

    parsed: list[tuple[str, _Trade]] = []
    for record in bybit_history:
        trade = _parse_bybit_trade(record, fx_rate_provider)
        parsed.append(("bybit", trade))
    for record in avanza_history:
        trade = _parse_avanza_trade(record, fx_rate_provider)
        if trade is not None:
            parsed.append(("avanza", trade))
    parsed.sort(key=lambda item: (item[1].timestamp, item[1].transaction_id))

    fifo_lots: dict[str, deque[_Lot]] = defaultdict(deque)
    average_lots: dict[str, _Lot] = {}
    output: list[dict[str, str]] = []

    for source, trade in parsed:
        instrument_key = trade.instrument.strip().upper()
        if source == "bybit":
            inventory = fifo_lots[instrument_key]
            if trade.side == "BUY":
                inventory.append(
                    _Lot(trade.quantity, trade.gross_sek + trade.fee_sek)
                )
                continue
            acquisition_cost = _consume_fifo(
                inventory, trade.quantity, trade.instrument
            )
        else:
            inventory = average_lots.setdefault(
                instrument_key, _Lot(Decimal("0"), Decimal("0"))
            )
            if trade.side == "BUY":
                inventory.quantity += trade.quantity
                inventory.cost_sek += trade.gross_sek + trade.fee_sek
                continue
            sale_proceeds = trade.gross_sek - trade.fee_sek
            if avanza_cost_method == "schablon":
                _consume_average(inventory, trade.quantity, trade.instrument)
                acquisition_cost = sale_proceeds * Decimal("0.20")
            else:
                acquisition_cost = _consume_average(
                    inventory, trade.quantity, trade.instrument
                )

        proceeds = trade.gross_sek - trade.fee_sek
        if proceeds < 0:
            raise ValueError(
                f"Fees exceed sale proceeds for {trade.instrument} "
                f"({trade.transaction_id})."
            )
        if report_start is not None and trade.timestamp.date() < report_start:
            continue
        transaction_id = trade.transaction_id or (
            f"{source}-{trade.timestamp.isoformat()}-{trade.instrument}"
        )
        output.append(
            {
                "transaction_id": transaction_id,
                "timestamp": trade.timestamp.isoformat().replace("+00:00", "Z"),
                "instrument": trade.instrument,
                "type": "SELL",
                "quantity": _format_decimal(trade.quantity),
                "price_native": _format_decimal(trade.price_native),
                "currency": trade.currency,
                "fx_rate_sek": _format_decimal(trade.fx_rate_sek),
                "total_sek": _format_decimal(proceeds),
                "acquisition_cost_sek": _format_decimal(acquisition_cost),
                "pnl_sek": _format_decimal(proceeds - acquisition_cost),
                "asset_type": "CRYPTO" if source == "bybit" else "EQUITY",
                "counterparty_type": trade.counterparty_type,
            }
        )
    return output


def _consume_fifo(
    lots: deque[_Lot], quantity: Decimal, instrument: str
) -> Decimal:
    remaining = quantity
    acquisition_cost = Decimal("0")
    while remaining > 0:
        if not lots:
            raise ValueError(
                f"Cannot match sale of {remaining} {instrument}; "
                "no earlier Bybit acquisition lot is available."
            )
        lot = lots[0]
        consumed = min(remaining, lot.quantity)
        cost = (
            lot.cost_sek
            if consumed == lot.quantity
            else lot.cost_sek * consumed / lot.quantity
        )
        acquisition_cost += cost
        remaining -= consumed
        lot.quantity -= consumed
        lot.cost_sek -= cost
        if lot.quantity == 0:
            lots.popleft()
    return acquisition_cost


def _consume_average(lot: _Lot, quantity: Decimal, instrument: str) -> Decimal:
    if quantity > lot.quantity:
        raise ValueError(
            f"Cannot match Avanza sale of {quantity} {instrument}; "
            f"only {lot.quantity} is covered by available acquisition history."
        )
    if lot.quantity == 0:
        raise ValueError(
            f"Cannot match Avanza sale of {instrument}; "
            "no earlier acquisition lot is available."
        )
    cost = lot.cost_sek * quantity / lot.quantity
    lot.quantity -= quantity
    lot.cost_sek -= cost
    return cost


def _format_decimal(value: Decimal) -> str:
    rounded = value.quantize(_CENT, rounding=ROUND_HALF_UP)
    return format(rounded, "f")


def write_k4_csv(
    rows: Iterable[Mapping[str, str]], output_path: str | Path
) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS, extrasaction="raise")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row[field] for field in CSV_FIELDS})
    return path


def build_dac8_rows(
    k4_rows: Iterable[Mapping[str, str]],
) -> list[dict[str, str]]:
    """Project K4 disposal data into a structured DAC8/CARF working ledger."""
    ledger: list[dict[str, str]] = []
    for row in k4_rows:
        timestamp = parse_timestamp(row.get("timestamp")).astimezone(timezone.utc)
        ledger.append(
            {
                "export_label": DAC8_EXPORT_LABEL,
                "timestamp_utc": timestamp.isoformat().replace("+00:00", "Z"),
                "asset_type": row["asset_type"],
                "transaction_hash_or_id": row["transaction_id"],
                "gross_proceeds_sek": row["total_sek"],
                "acquisition_cost_sek": row["acquisition_cost_sek"],
                "realized_gain_loss_sek": row["pnl_sek"],
                "counterparty_type": row["counterparty_type"],
            }
        )
    return ledger


def write_dac8_csv(
    rows: Iterable[Mapping[str, str]], output_path: str | Path
) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(
            csv_file, fieldnames=DAC8_CSV_FIELDS, extrasaction="raise"
        )
        writer.writeheader()
        writer.writerows(rows)
    return path


def write_dac8_json(
    rows: Iterable[Mapping[str, str]], output_path: str | Path
) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"export_label": DAC8_EXPORT_LABEL, "transactions": list(rows)}
    with path.open("w", encoding="utf-8") as json_file:
        json.dump(payload, json_file, ensure_ascii=False, indent=2)
        json_file.write("\n")
    return path


def write_k4_pdf(rows: Iterable[Mapping[str, str]], output_path: str | Path) -> Path:
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import landscape, letter
        from reportlab.platypus import SimpleDocTemplate, Spacer, Table, TableStyle, Paragraph
        from reportlab.lib.styles import getSampleStyleSheet
    except ImportError as error:
        raise RuntimeError("K4 PDF export requires the reportlab dependency.") from error

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = SimpleDocTemplate(str(path), pagesize=landscape(letter))
    styles = getSampleStyleSheet()
    table_rows = [[field.replace("_", " ").title() for field in CSV_FIELDS]]
    table_rows.extend(
        [[str(row[field]) for field in CSV_FIELDS] for row in rows]
    )
    table = Table(table_rows, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#d8b4fe")),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("FONTSIZE", (0, 0), (-1, -1), 7),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    document.build(
        [
            Paragraph("K4 Working Report", styles["Title"]),
            Paragraph(
                "Review classifications, cost basis, fees, and FX rates before filing. "
                "This report is not an official Skatteverket form.",
                styles["BodyText"],
            ),
            Spacer(1, 12),
            table,
        ]
    )
    return path


def _load_fx_rates(path: Path) -> FxRateProvider:
    rates: dict[tuple[str, date], Decimal] = {}
    with path.open(newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        required = {"date", "currency", "fx_rate_sek"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError(
                "FX CSV must contain the columns: date, currency, fx_rate_sek."
            )
        for row in reader:
            rate_date = date.fromisoformat(row["date"])
            currency = row["currency"].strip().upper()
            if not currency:
                raise ValueError("FX CSV contains an empty currency.")
            rates[(currency, rate_date)] = _decimal(
                row["fx_rate_sek"], f"{currency}/SEK rate", positive=True
            )

    def lookup(currency: str, timestamp: datetime) -> Decimal:
        applicable = [
            rate_date
            for (rate_currency, rate_date) in rates
            if rate_currency == currency and rate_date <= timestamp.date()
        ]
        if not applicable:
            raise ValueError(
                f"FX CSV has no {currency}/SEK rate on or before "
                f"{timestamp.date().isoformat()}."
            )
        return rates[(currency, max(applicable))]

    return lookup


def export_tax_csv(
    output_path: str | Path,
    *,
    bybit_session: object,
    avanza_session: object,
    start_date: date,
    end_date: date,
    fx_rate_provider: FxRateProvider | None = None,
    avanza_cost_method: AvanzaCostMethod = "average",
    report_start: date | None = None,
) -> tuple[Path, list[dict[str, str]]]:
    bybit_history = fetch_bybit_history(bybit_session, start_date, end_date)
    avanza_history = fetch_avanza_history(avanza_session, start_date, end_date)
    rows = build_k4_rows(
        bybit_history,
        avanza_history,
        fx_rate_provider=fx_rate_provider,
        avanza_cost_method=avanza_cost_method,
        report_start=report_start,
    )
    return write_k4_csv(rows, output_path), rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export completed Bybit and Avanza trades as a K4-oriented CSV."
    )
    parser.add_argument("--from-date", type=date.fromisoformat, required=True)
    parser.add_argument("--to-date", type=date.fromisoformat, required=True)
    parser.add_argument(
        "--report-from",
        type=date.fromisoformat,
        help="Only include disposals on or after this date; earlier history is "
        "still used to calculate cost basis.",
    )
    parser.add_argument("--output", type=Path, default=Path("K4.csv"))
    parser.add_argument(
        "--pdf-output",
        type=Path,
        help="Also write a printable K4 working-report PDF.",
    )
    parser.add_argument(
        "--dac8-output",
        type=Path,
        help="Write the DAC8/CARF Transaction Ledger Export as CSV or JSON.",
    )
    parser.add_argument(
        "--dac8-format", choices=("csv", "json"), default="csv"
    )
    parser.add_argument("--fx-rates", type=Path)
    parser.add_argument(
        "--avanza-cost-method",
        choices=("average", "schablon"),
        default="average",
        help="Use Genomsnittsmetoden (average) or the 20%% Schablonmetoden.",
    )
    parser.add_argument(
        "--allow-testnet",
        action="store_true",
        help="Allow a Bybit testnet session (normally refused for tax exports).",
    )
    args = parser.parse_args()
    if args.from_date > args.to_date:
        parser.error("--from-date must be on or before --to-date")
    if args.report_from and args.report_from > args.to_date:
        parser.error("--report-from must be on or before --to-date")

    sessions = SessionsManager()
    bybit_session = sessions.get_bybit_session()
    if getattr(bybit_session, "testnet", None) is True and not args.allow_testnet:
        raise RuntimeError(
            "Bybit is configured for testnet. Set BYBIT_TESTNET=false for a tax "
            "export or pass --allow-testnet only for a test run."
        )
    avanza_session = sessions.get_avanza_session()
    fx_provider = _load_fx_rates(args.fx_rates) if args.fx_rates else None

    path, rows = export_tax_csv(
        args.output,
        bybit_session=bybit_session,
        avanza_session=avanza_session,
        start_date=args.from_date,
        end_date=args.to_date,
        fx_rate_provider=fx_provider,
        avanza_cost_method=args.avanza_cost_method,
        report_start=args.report_from,
    )
    net_gain = sum(
        (Decimal(row["pnl_sek"]) for row in rows if Decimal(row["pnl_sek"]) > 0),
        Decimal("0"),
    )
    net_loss = sum(
        (Decimal(row["pnl_sek"]) for row in rows if Decimal(row["pnl_sek"]) < 0),
        Decimal("0"),
    )
    logger.info(
        "Wrote %d K4 disposal rows to %s (gains: %s SEK, losses: %s SEK, net: %s SEK).",
        len(rows),
        path,
        _format_decimal(net_gain),
        _format_decimal(net_loss),
        _format_decimal(net_gain + net_loss),
    )
    if args.pdf_output:
        pdf_path = write_k4_pdf(rows, args.pdf_output)
        logger.info("Wrote K4 working-report PDF to %s.", pdf_path)
    if args.dac8_output:
        ledger = build_dac8_rows(rows)
        writer = write_dac8_csv if args.dac8_format == "csv" else write_dac8_json
        ledger_path = writer(ledger, args.dac8_output)
        logger.info(
            "Wrote %s (%d records) to %s. Confirm applicable DAC8/CARF requirements "
            "with a qualified tax professional.",
            DAC8_EXPORT_LABEL,
            len(ledger),
            ledger_path,
        )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    main()
