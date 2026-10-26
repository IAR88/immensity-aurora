from __future__ import annotations

import json
import logging
import math
import os
import threading
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TradeOutcome:
    trade_id: str
    close_execution_id: str
    closed_at: str
    symbol: str
    signal_ids: tuple[str, ...]
    side: str
    quantity: float
    entry_price: float
    exit_price: float
    entry_value: float
    net_pnl: float
    net_return_pct: float
    expected_value_pct: float
    analysis: str


@dataclass(slots=True)
class _OrderSignal:
    symbol: str
    signal_ids: tuple[str, ...]
    expected_value_pct: float


@dataclass(slots=True)
class _OpenLot:
    trade_id: str
    entry_order_link_id: str
    symbol: str
    signal_ids: tuple[str, ...]
    expected_value_pct: float
    entry_quantity: float
    quantity_remaining: float
    entry_price: float
    entry_value_per_unit: float
    entry_value: float
    entry_value_remaining: float
    exit_quantity: float = 0.0
    exit_value: float = 0.0
    realized_net_pnl: float = 0.0


class TradeAnalyzer:
    """Match observed spot fills and persist fully closed, locally tracked trades."""

    def __init__(
        self,
        path: str | Path = "data/trade_outcomes.jsonl",
        *,
        on_trade_closed: Callable[[TradeOutcome], None] | None = None,
    ) -> None:
        self.path = Path(path)
        self.on_trade_closed = on_trade_closed
        self._lock = threading.RLock()
        self._orders: dict[str, _OrderSignal] = {}
        self._lots: dict[str, deque[_OpenLot]] = defaultdict(deque)
        self._seen_execution_ids: set[str] = set()
        self._outcomes = self._load_outcomes()
        self._closed_execution_ids = {
            outcome.close_execution_id for outcome in self._outcomes
        }
        self._load_state()

    def register_order(
        self,
        order_link_id: str,
        *,
        symbol: str,
        signal_ids: tuple[str, ...],
        expected_value_pct: float,
    ) -> None:
        if not order_link_id or not symbol or not signal_ids:
            raise ValueError("Order correlation requires an ID, symbol, and signal IDs.")
        expected_value = self._finite_number(expected_value_pct, "expected value")
        with self._lock:
            self._orders[order_link_id] = _OrderSignal(
                symbol=symbol,
                signal_ids=signal_ids,
                expected_value_pct=expected_value,
            )
            self._persist_state()

    def unregister_order(self, order_link_id: str) -> None:
        with self._lock:
            self._orders.pop(order_link_id, None)
            self._persist_state()

    def record_execution(self, message: object) -> list[TradeOutcome]:
        if not isinstance(message, Mapping):
            raise ValueError("Bybit execution message must be an object.")
        executions = message.get("data")
        if not isinstance(executions, list):
            raise ValueError("Bybit execution message must contain a data list.")

        outcomes: list[TradeOutcome] = []
        for execution in executions:
            if not isinstance(execution, Mapping):
                raise ValueError("Bybit execution item must be an object.")
            category = execution.get("category")
            if category not in (None, "", "spot"):
                continue
            outcome = self._record_spot_execution(execution)
            if outcome is not None:
                outcomes.extend(outcome)
        return outcomes

    def _record_spot_execution(
        self, execution: Mapping[str, object]
    ) -> list[TradeOutcome]:
        execution_id = str(execution.get("execId") or "")
        symbol = str(execution.get("symbol") or "")
        side = execution.get("side")
        quantity = self._finite_number(execution.get("execQty"), "execution quantity")
        price = self._finite_number(execution.get("execPrice"), "execution price")
        fee = self._finite_number(execution.get("execFee", 0), "execution fee")
        fee_currency = str(execution.get("feeCurrency") or "")
        if not execution_id or not symbol or side not in {"Buy", "Sell"}:
            raise ValueError("Bybit spot execution is missing ID, symbol, or side.")
        if quantity <= 0 or price <= 0 or fee < 0:
            raise ValueError("Bybit spot execution quantity, price, or fee is invalid.")

        quote_currency = next(
            (
                suffix
                for suffix in ("USDT", "USDC", "USDE", "BTC", "ETH", "EUR")
                if symbol.endswith(suffix)
            ),
            None,
        )
        if quote_currency is None or len(symbol) <= len(quote_currency):
            raise ValueError(f"Unsupported quote currency for spot symbol {symbol}.")
        base_currency = symbol.removesuffix(quote_currency)
        fee_in_quote = 0.0
        fee_in_base = 0.0
        if fee_currency == quote_currency:
            fee_in_quote = fee
        elif fee_currency == base_currency:
            fee_in_base = fee
        elif fee:
            raise ValueError(
                f"Cannot value {fee_currency} execution fees for {symbol}."
            )

        with self._lock:
            if execution_id in self._seen_execution_ids:
                return []
            self._seen_execution_ids.add(execution_id)
            if execution_id in self._closed_execution_ids:
                self._persist_state()
                return []

            if side == "Buy":
                order_link_id = str(execution.get("orderLinkId") or "")
                order = self._orders.get(order_link_id)
                if order is None:
                    logger.warning(
                        "Ignoring unmatched Bybit buy fill %s for %s; no signal metadata.",
                        execution_id,
                        symbol,
                    )
                    self._persist_state()
                    return []
                if order.symbol != symbol:
                    raise ValueError("Execution symbol does not match its signal order.")
                acquired_quantity = quantity - fee_in_base
                if acquired_quantity <= 0:
                    raise ValueError("Buy execution fee consumed the entire fill.")
                entry_value = quantity * price + fee_in_quote
                existing_lot = next(
                    (
                        lot
                        for lot in self._lots[symbol]
                        if lot.entry_order_link_id == order_link_id
                    ),
                    None,
                )
                if existing_lot is None:
                    self._lots[symbol].append(
                        _OpenLot(
                            trade_id=order_link_id,
                            entry_order_link_id=order_link_id,
                            symbol=symbol,
                            signal_ids=order.signal_ids,
                            expected_value_pct=order.expected_value_pct,
                            entry_quantity=quantity,
                            quantity_remaining=acquired_quantity,
                            entry_price=price,
                            entry_value_per_unit=entry_value / acquired_quantity,
                            entry_value=entry_value,
                            entry_value_remaining=entry_value,
                        )
                    )
                else:
                    old_quantity = existing_lot.quantity_remaining
                    old_entry_value = existing_lot.entry_value_remaining
                    new_quantity = old_quantity + acquired_quantity
                    existing_lot.entry_price = (
                        existing_lot.entry_price * existing_lot.entry_quantity
                        + price * quantity
                    ) / (existing_lot.entry_quantity + quantity)
                    existing_lot.entry_quantity += quantity
                    existing_lot.quantity_remaining = new_quantity
                    existing_lot.entry_value += entry_value
                    existing_lot.entry_value_remaining = old_entry_value + entry_value
                    existing_lot.entry_value_per_unit = (
                        existing_lot.entry_value_remaining / new_quantity
                    )
                self._persist_state()
                return []

            outcomes = self._match_sell(
                execution=execution,
                execution_id=execution_id,
                symbol=symbol,
                quantity=quantity,
                price=price,
                fee_in_quote=fee_in_quote,
                fee_in_base=fee_in_base,
            )
            self._persist_state()
        if self.on_trade_closed is not None:
            for outcome in outcomes:
                self.on_trade_closed(outcome)
        return outcomes

    def _match_sell(
        self,
        *,
        execution: Mapping[str, object],
        execution_id: str,
        symbol: str,
        quantity: float,
        price: float,
        fee_in_quote: float,
        fee_in_base: float,
    ) -> list[TradeOutcome]:
        lots = self._lots[symbol]
        quantity_to_match = quantity + fee_in_base
        total_quantity_to_match = quantity_to_match
        proceeds = quantity * price - fee_in_quote - fee_in_base * price
        if not lots:
            logger.warning(
                "Ignoring unmatched Bybit sell fill %s for %s; entry fill was not observed.",
                execution_id,
                symbol,
            )
            return []

        outcomes: list[TradeOutcome] = []
        while quantity_to_match > 1e-12 and lots:
            lot = lots[0]
            matched_quantity = min(quantity_to_match, lot.quantity_remaining)
            proportion = matched_quantity / total_quantity_to_match
            sold_quantity = quantity * proportion
            matched_proceeds = proceeds * proportion
            entry_cost = matched_quantity * lot.entry_value_per_unit
            lot.quantity_remaining -= matched_quantity
            lot.entry_value_remaining -= entry_cost
            lot.exit_quantity += sold_quantity
            lot.exit_value += sold_quantity * price
            lot.realized_net_pnl += matched_proceeds - entry_cost
            quantity_to_match -= matched_quantity
            if lot.quantity_remaining <= 1e-12:
                lots.popleft()
                outcomes.append(self._close_lot(lot, execution, execution_id))

        if quantity_to_match > 1e-12:
            logger.warning(
                "Bybit sell fill %s exceeded observed entry quantity for %s by %.12g.",
                execution_id,
                symbol,
                quantity_to_match,
            )
        return outcomes

    def _close_lot(
        self,
        lot: _OpenLot,
        execution: Mapping[str, object],
        close_execution_id: str,
    ) -> TradeOutcome:
        if lot.exit_quantity <= 0 or lot.entry_value <= 0:
            raise ValueError("Cannot analyze a trade without matched entry and exit fills.")
        closed_at = self._execution_time(execution.get("execTime"))
        net_return = lot.realized_net_pnl / lot.entry_value
        analysis = self._analysis_summary(
            lot.signal_ids,
            net_return,
            lot.realized_net_pnl,
        )
        outcome = TradeOutcome(
            trade_id=lot.trade_id,
            close_execution_id=close_execution_id,
            closed_at=closed_at,
            symbol=lot.symbol,
            signal_ids=lot.signal_ids,
            side="Buy",
            quantity=lot.exit_quantity,
            entry_price=lot.entry_price,
            exit_price=lot.exit_value / lot.exit_quantity,
            entry_value=lot.entry_value,
            net_pnl=lot.realized_net_pnl,
            net_return_pct=net_return * 100,
            expected_value_pct=lot.expected_value_pct,
            analysis=analysis,
        )
        self._append_outcome(outcome)
        self._outcomes.append(outcome)
        self._closed_execution_ids.add(close_execution_id)
        return outcome

    def _analysis_summary(
        self, signal_ids: tuple[str, ...], current_return: float, current_net: float
    ) -> str:
        summaries: list[str] = []
        for signal_id in signal_ids:
            prior = [
                outcome
                for outcome in self._outcomes
                if signal_id in outcome.signal_ids
            ]
            if not prior:
                summaries.append(
                    f"{signal_id}: first recorded result; current net "
                    f"{current_net:.8g} ({current_return:.2%})."
                )
                continue
            wins = sum(outcome.net_pnl > 0 for outcome in prior)
            average_return = sum(
                outcome.net_return_pct for outcome in prior
            ) / len(prior)
            summaries.append(
                f"{signal_id}: prior {len(prior)} trades, win rate "
                f"{wins / len(prior):.1%}, average net return {average_return:.2f}%; "
                f"current {current_return:.2%}."
            )
        return " ".join(summaries)

    def _append_outcome(self, outcome: TradeOutcome) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        serialized = asdict(outcome)
        serialized["signal_ids"] = list(outcome.signal_ids)
        with self.path.open("a", encoding="utf-8") as record_file:
            record_file.write(json.dumps(serialized, sort_keys=True) + "\n")

    def _load_outcomes(self) -> list[TradeOutcome]:
        if not self.path.exists():
            return []
        outcomes: list[TradeOutcome] = []
        with self.path.open(encoding="utf-8") as record_file:
            for line_number, line in enumerate(record_file, 1):
                try:
                    record = json.loads(line)
                    record["signal_ids"] = tuple(record["signal_ids"])
                    outcomes.append(TradeOutcome(**record))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                    raise ValueError(
                        f"Invalid trade outcome record at {self.path}:{line_number}."
                    ) from error
        return outcomes

    @property
    def _state_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.state.json")

    def _persist_state(self) -> None:
        state_path = self._state_path
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "version": 2,
            "orders": {
                link_id: asdict(order) for link_id, order in self._orders.items()
            },
            "lots": {
                symbol: [asdict(lot) for lot in lots]
                for symbol, lots in self._lots.items()
            },
            "seen_execution_ids": sorted(self._seen_execution_ids),
        }
        temporary_path = state_path.with_name(f"{state_path.name}.tmp")
        with temporary_path.open("w", encoding="utf-8") as state_file:
            json.dump(state, state_file, sort_keys=True)
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temporary_path, state_path)

    def _load_state(self) -> None:
        if not self._state_path.exists():
            return
        try:
            with self._state_path.open(encoding="utf-8") as state_file:
                state = json.load(state_file)
            if not isinstance(state, Mapping) or state.get("version") != 2:
                raise ValueError("Unsupported trade analyzer state format.")
            orders = state.get("orders")
            lots = state.get("lots")
            seen_execution_ids = state.get("seen_execution_ids")
            if not isinstance(orders, Mapping) or not isinstance(lots, Mapping):
                raise ValueError("Trade analyzer state is missing orders or lots.")
            if not isinstance(seen_execution_ids, list) or any(
                not isinstance(execution_id, str)
                for execution_id in seen_execution_ids
            ):
                raise ValueError("Trade analyzer state has invalid execution IDs.")
            for link_id, record in orders.items():
                if not isinstance(link_id, str) or not isinstance(record, Mapping):
                    raise ValueError("Trade analyzer state has invalid order metadata.")
                self._orders[link_id] = _OrderSignal(
                    symbol=str(record["symbol"]),
                    signal_ids=tuple(record["signal_ids"]),
                    expected_value_pct=self._finite_number(
                        record["expected_value_pct"], "expected value"
                    ),
                )
            for symbol, records in lots.items():
                if not isinstance(symbol, str) or not isinstance(records, list):
                    raise ValueError("Trade analyzer state has invalid open lots.")
                for record in records:
                    if not isinstance(record, Mapping):
                        raise ValueError("Trade analyzer state has invalid lot data.")
                    lot_data = dict(record)
                    lot_data["signal_ids"] = tuple(lot_data["signal_ids"])
                    self._lots[symbol].append(_OpenLot(**lot_data))
            self._seen_execution_ids.update(seen_execution_ids)
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
            raise ValueError(
                f"Could not read trade analyzer state at {self._state_path}."
            ) from error

    @staticmethod
    def _execution_time(value: object) -> str:
        if value in (None, ""):
            timestamp = datetime.now(timezone.utc)
        else:
            milliseconds = TradeAnalyzer._finite_number(value, "execution time")
            timestamp = datetime.fromtimestamp(milliseconds / 1000, timezone.utc)
        return timestamp.isoformat()

    @staticmethod
    def _finite_number(value: object, field_name: str) -> float:
        if isinstance(value, bool):
            raise ValueError(f"{field_name} must be finite.")
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{field_name} must be finite.") from error
        if not math.isfinite(number):
            raise ValueError(f"{field_name} must be finite.")
        return number
