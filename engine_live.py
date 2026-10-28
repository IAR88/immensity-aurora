from __future__ import annotations

import json
import logging
import math
import os
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import requests
from dotenv import load_dotenv
from pybit.exceptions import FailedRequestError

from auth.sessions_manager import (
    MockBybitSession,
    SessionsManager,
    bybit_api_domain_parts,
)
from bybit_execution import ExecutionStreamListener
from engine.bybit_client import BybitClient
from engine.circuit_breaker import CircuitBreaker
from engine.p1_p15_converter import (
    P1P15Converter,
    PREDICTIVE_PARAMETER_NAMES as ENGINE_PREDICTIVE_PARAMETER_NAMES,
)
from engine.opportunity_optimizer import OpportunityCostOptimizer
from engine.regime_selector import RegimeSelector
from engine.ring_buffer import RingBuffer
from trade_analysis import TradeAnalyzer, TradeOutcome

load_dotenv()

logger = logging.getLogger(__name__)

TECHNICAL_SIGNALS = frozenset(f"P{number}" for number in range(1, 16))
PREDICTIVE_PARAMETER_NAMES = ENGINE_PREDICTIVE_PARAMETER_NAMES
MIN_EXECUTION_WIN_PROBABILITY = 0.80
HIGH_CONFLUENCE_THRESHOLD = 0.90


def is_bybit_ip_restriction(error: BaseException) -> bool:
    status_code = getattr(error, "status_code", None)
    if status_code is None:
        status_code = getattr(getattr(error, "response", None), "status_code", None)
    return status_code == 403 or status_code == "403"


def mock_predictive_parameters() -> dict[str, float]:
    """Normalized offline fixture data; it is not verified or executable."""
    parameters = {name: 1.0 for name in PREDICTIVE_PARAMETER_NAMES.values()}
    parameters["public_reaction_speed"] = 0.0
    return parameters


@dataclass(frozen=True, slots=True)
class ProbabilityFeatures:
    """Direction-adjusted CVD and momentum plus normalized volatility risk.

    Each value must be between 0 and 1; higher CVD and momentum are favorable,
    while higher volatility is less favorable.
    """

    cvd: float
    momentum: float
    volatility: float

    def score(self) -> float:
        cvd = HjarnanLiveEngine._finite_number(self.cvd, "feature cvd")
        momentum = HjarnanLiveEngine._finite_number(self.momentum, "feature momentum")
        volatility = HjarnanLiveEngine._finite_number(
            self.volatility, "feature volatility"
        )
        if any(not 0 <= value <= 1 for value in (cvd, momentum, volatility)):
            raise ValueError("Probability features must be normalized to [0, 1].")
        return 0.4 * cvd + 0.4 * momentum + 0.2 * (1 - volatility)


@dataclass(frozen=True, slots=True)
class CalibrationObservation:
    features: ProbabilityFeatures
    outcome: int


class IsotonicCalibrator:
    """Small dependency-free isotonic regression fitted with PAV."""

    def __init__(self, observations: Iterable[CalibrationObservation]) -> None:
        samples: list[tuple[float, int]] = []
        for observation in observations:
            if not isinstance(observation, CalibrationObservation):
                raise TypeError("Calibration data must contain labeled observations.")
            if type(observation.outcome) is not int or observation.outcome not in (
                0,
                1,
            ):
                raise ValueError("Calibration outcomes must be 0 or 1.")
            samples.append((observation.features.score(), observation.outcome))
        if len(samples) < 2 or {outcome for _, outcome in samples} != {0, 1}:
            raise ValueError(
                "Isotonic calibration needs at least two observations and both outcomes."
            )

        grouped: list[list[float | int]] = []
        for score, outcome in sorted(samples):
            if grouped and grouped[-1][0] == score:
                grouped[-1][2] += outcome
                grouped[-1][3] += 1
            else:
                grouped.append([score, score, outcome, 1])

        blocks: list[list[float | int]] = []
        for group in grouped:
            blocks.append(group.copy())
            while len(blocks) > 1:
                previous, current = blocks[-2:]
                previous_mean = previous[2] / previous[3]
                current_mean = current[2] / current[3]
                if previous_mean <= current_mean:
                    break
                blocks[-2:] = [
                    [
                        previous[0],
                        current[1],
                        previous[2] + current[2],
                        previous[3] + current[3],
                    ]
                ]
        self._blocks = [
            (float(block[1]), float(block[2]) / int(block[3])) for block in blocks
        ]

    def predict(self, score: float) -> float:
        score = HjarnanLiveEngine._finite_number(score, "calibration score")
        if not 0 <= score <= 1:
            raise ValueError("Calibration score must be between 0 and 1.")
        return next(
            (
                probability
                for upper_bound, probability in self._blocks
                if score <= upper_bound
            ),
            self._blocks[-1][1],
        )


@dataclass(frozen=True, slots=True)
class LiveTradeSignal:
    """A complete, externally verified spot-trading proposal.

    Rates are decimals (0.10 means 10%). All fifteen P-signals must be
    explicitly true; this engine does not invent or interpret signal data.
    """

    symbol: str
    category: Literal["spot"]
    side: Literal["Buy", "Sell"]
    entry_price: float
    win_probability: float
    probability_features: ProbabilityFeatures
    take_profit_pct: float
    stop_loss_pct: float
    estimated_cost_pct: float
    expected_value_pct: float
    expected_value_verified: bool
    orderbook_bid_depth_usdt: float
    orderbook_ask_depth_usdt: float
    orderbook_verified: bool
    cvd: float
    cvd_verified: bool
    technical_signals: Mapping[str, bool]
    predictive_parameters: Mapping[str, float] | None = None


@dataclass(frozen=True, slots=True)
class AvanzaPosition:
    symbol: str
    quantity: float
    market_value: float
    currency: str = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class AvanzaCashPosition:
    amount: float
    currency: str


@dataclass(frozen=True, slots=True)
class AvanzaPortfolioSnapshot:
    positions: tuple[AvanzaPosition, ...]
    cash_positions: tuple[AvanzaCashPosition, ...]
    available: bool


def _avanza_number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, Mapping):
        for key in ("amount", "value", "raw", "total", "balance"):
            if key in value:
                number = _avanza_number(value[key])
                if number is not None:
                    return number
        return None
    if not isinstance(value, str):
        return None

    normalized = value.replace("\u00a0", "").replace(" ", "").strip()
    if "," in normalized:
        if "." in normalized and normalized.rfind(",") < normalized.rfind("."):
            normalized = normalized.replace(",", "")
        else:
            normalized = normalized.replace(".", "").replace(",", ".")
    try:
        number = float(normalized)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _nested_avanza_value(value: object, aliases: frozenset[str]) -> object | None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized_key = "".join(
                character.lower()
                for character in str(key)
                if character.isalnum()
            )
            if normalized_key in aliases:
                return child
        for child in value.values():
            found = _nested_avanza_value(child, aliases)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _nested_avanza_value(child, aliases)
            if found is not None:
                return found
    return None


def load_avanza_portfolio(
    path: Path = Path("avanza_positions.json"),
) -> AvanzaPortfolioSnapshot:
    try:
        with path.open(encoding="utf-8") as positions_file:
            payload = json.load(positions_file)
    except FileNotFoundError:
        logger.warning("Avanza positions file %s was not found.", path)
        return AvanzaPortfolioSnapshot((), (), False)

    position_rows: list[Mapping[str, object]] = []
    cash_rows: list[object] = []
    if isinstance(payload, list):
        position_rows = [row for row in payload if isinstance(row, Mapping)]
    elif isinstance(payload, Mapping):
        position_keys = ("positions", "holdings", "withOrderbook", "withoutOrderbook")
        found_positions = False
        for key in position_keys:
            rows = payload.get(key)
            if isinstance(rows, list):
                found_positions = True
                position_rows.extend(row for row in rows if isinstance(row, Mapping))
        raw_cash_rows = payload.get("cashPositions", [])
        if isinstance(raw_cash_rows, list):
            cash_rows = raw_cash_rows
        elif raw_cash_rows is not None:
            raise TypeError("Avanza cash positions must be a list.")
        if not found_positions and "cashPositions" not in payload:
            raise ValueError("Avanza response did not contain a positions list.")
    else:
        raise TypeError("Avanza positions response must be a list or mapping.")

    positions: list[AvanzaPosition] = []
    for row in position_rows:
        instrument = row.get("instrument")
        if not isinstance(instrument, Mapping):
            instrument = {}
        orderbook = instrument.get("orderbook")
        if not isinstance(orderbook, Mapping):
            orderbook = {}
        symbol_value = (
            row.get("symbol")
            or row.get("tickerSymbol")
            or row.get("orderbookId")
            or orderbook.get("id")
            or instrument.get("isin")
            or instrument.get("name")
            or row.get("name")
        )
        quantity_value = row.get("quantity", row.get("volume"))
        market_value = row.get("marketValue", row.get("value"))
        if isinstance(quantity_value, Mapping):
            quantity_value = quantity_value.get("value")
        if isinstance(market_value, Mapping):
            market_value = market_value.get("value")
        if symbol_value is None or quantity_value is None or market_value is None:
            raise ValueError(
                "Avanza position is missing symbol, quantity, or market value."
            )
        currency_value = (
            row.get("currencyCode")
            or row.get("currency")
            or orderbook.get("currencyCode")
            or orderbook.get("currency")
            or instrument.get("currency")
        )
        positions.append(
            AvanzaPosition(
                symbol=str(symbol_value),
                quantity=_finite_avanza_number(
                    quantity_value, "position quantity"
                ),
                market_value=_finite_avanza_number(
                    market_value, "position market value"
                ),
                currency=str(currency_value).upper() if currency_value else "UNKNOWN",
            )
        )

    cash_positions: list[AvanzaCashPosition] = []
    amount_aliases = frozenset(
        {"balance", "amount", "cashvalue", "cash", "availablecash", "value"}
    )
    currency_aliases = frozenset({"currencycode", "currency", "currencyname"})
    for row in cash_rows:
        amount_value = _nested_avanza_value(row, amount_aliases)
        if amount_value is None:
            continue
        amount = _avanza_number(amount_value)
        if amount is None:
            raise ValueError("Avanza cash position contains an invalid amount.")
        currency_value = _nested_avanza_value(row, currency_aliases)
        currency = str(currency_value).strip().upper() if currency_value else "UNKNOWN"
        cash_positions.append(AvanzaCashPosition(amount=amount, currency=currency))

    return AvanzaPortfolioSnapshot(tuple(positions), tuple(cash_positions), True)


def _finite_avanza_number(value: object, label: str) -> float:
    number = _avanza_number(value)
    if number is None:
        raise ValueError(f"Avanza {label} must be a finite number.")
    return number


@dataclass(frozen=True, slots=True)
class RotationCandidate:
    action: Literal["buy", "sell"]
    symbol: str
    reason: str
    news_impact_score: float = 0.0


@dataclass(frozen=True, slots=True)
class ManagedSpotPosition:
    symbol: str
    entry_price: float
    peak_price: float
    stop_price: float


class HjarnanLiveEngine:
    """Fail-closed Bybit spot execution and read-only Avanza monitoring."""

    target_tp_pct = 0.10
    target_sl_pct = 0.05
    circuit_breaker_pct = -0.02
    target_trades_per_day = 6
    max_trades_per_day = 10

    def __init__(
        self,
        sessions_manager: SessionsManager | None = None,
        *,
        daily_start_equity_usdt: float | None = None,
        min_orderbook_depth_usdt: float = 1_000.0,
        calibration_observations: Iterable[CalibrationObservation] | None = None,
        trade_analyzer: TradeAnalyzer | None = None,
        avanza_positions_file: Path | None = None,
    ) -> None:
        self.sessions = sessions_manager or SessionsManager()
        self.bybit_session = self.sessions.get_bybit_session()
        self.avanza_positions_file = avanza_positions_file or Path(
            os.getenv("AVANZA_POSITIONS_FILE", "avanza_positions.json")
        )
        self.mock_mode = bool(getattr(self.sessions, "mock_mode", False))
        self.bybit_ip_restricted = False
        self.bybit_testnet = self._read_testnet_flag(self.bybit_session)
        domain, tld = bybit_api_domain_parts(
            os.getenv("BYBIT_DOMAIN", "bybit.com")
        )
        self.bybit_endpoint = f"https://api.{domain}.{tld}"
        if self.mock_mode:
            self.bybit_client: BybitClient | None = None
        else:
            credentials = self.sessions.get_bybit_credentials()
            api_key = credentials.get("api_key")
            api_secret = credentials.get("api_secret")
            if not isinstance(api_key, str) or not isinstance(api_secret, str):
                raise ValueError("Bybit credentials are required for REST trading.")
            self.bybit_client = BybitClient(
                api_key,
                api_secret,
                testnet=self.bybit_testnet,
                domain=os.getenv("BYBIT_DOMAIN", "bybit.com"),
            )
        self.allow_mainnet_trading = self._env_bool(
            "BYBIT_ALLOW_MAINNET_TRADING", default=False
        )
        if daily_start_equity_usdt is None:
            configured_equity = os.getenv("BYBIT_DAILY_START_EQUITY_USDT")
            daily_start_equity_usdt = (
                float(configured_equity) if configured_equity is not None else None
            )
        if daily_start_equity_usdt is not None and (
            not math.isfinite(daily_start_equity_usdt) or daily_start_equity_usdt <= 0
        ):
            raise ValueError("Daily starting equity must be a positive finite amount.")
        if not math.isfinite(min_orderbook_depth_usdt) or min_orderbook_depth_usdt < 0:
            raise ValueError("Minimum orderbook depth must be finite and non-negative.")

        self.daily_start_equity_usdt = daily_start_equity_usdt
        self.min_orderbook_depth_usdt = min_orderbook_depth_usdt
        self._equity_date: date | None = None
        self._daily_halted = False
        self._daily_trade_count = 0
        config_path = Path(__file__).resolve().parent / "config" / "sesamfro_graph.json"
        self.regime_selector = RegimeSelector.from_file(config_path)
        self.circuit_breaker = CircuitBreaker(
            max_drawdown_pct=self.circuit_breaker_pct,
            p14_threshold=self.regime_selector.p14_threshold,
        )
        self.opportunity_optimizer = OpportunityCostOptimizer()
        self.equity_history: RingBuffer[float] = RingBuffer(capacity=512)
        self.signal_score_history: RingBuffer[float] = RingBuffer(capacity=512)
        self.probability_calibrator = (
            IsotonicCalibrator(calibration_observations)
            if calibration_observations is not None
            else None
        )
        self.telegram_token = os.getenv("TELEGRAM_TOKEN") or os.getenv(
            "TELEGRAM_BOT_TOKEN"
        )
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.ntfy_topic = os.getenv("NTFY_TOPIC", "").strip()
        self.ntfy_server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        self.ntfy_access_token = os.getenv("NTFY_ACCESS_TOKEN", "").strip()
        self._ntfy_configuration_warned = False
        self._last_portfolio_signature: tuple[object, ...] | None = None
        self._last_signal_signatures: dict[str, str] = {}
        analysis_path = os.getenv("TRADE_ANALYSIS_PATH") or "data/trade_outcomes.jsonl"
        self.trade_analyzer = trade_analyzer or TradeAnalyzer(
            analysis_path, on_trade_closed=self._notify_trade_closed
        )

    @staticmethod
    def _read_testnet_flag(session: object) -> bool:
        testnet = getattr(session, "testnet", None)
        if not isinstance(testnet, bool):
            raise RuntimeError(
                "Bybit session does not expose its testnet state; refusing order access."
            )
        return testnet

    def _activate_simulated_mode(self) -> None:
        free_usdt = float(os.getenv("MOCK_BYBIT_FREE_USDT", "200"))
        if not math.isfinite(free_usdt) or free_usdt < 0:
            raise ValueError(
                "MOCK_BYBIT_FREE_USDT must be finite and non-negative."
            )
        self.mock_mode = True
        self.bybit_ip_restricted = True
        self.bybit_session = MockBybitSession(free_usdt)
        self.bybit_testnet = True
        logger.warning(
            "Bybit returned HTTP 403; switching to simulated data and disabling orders."
        )

    def _bybit_request(
        self, method_name: str, *args: object, **kwargs: object
    ) -> dict[str, object]:
        method = getattr(self.bybit_session, method_name)
        try:
            response = method(*args, **kwargs)
        except (FailedRequestError, requests.exceptions.HTTPError) as error:
            if not is_bybit_ip_restriction(error):
                raise
            self._activate_simulated_mode()
            if method_name == "get_wallet_balance":
                response = self.bybit_session.get_wallet_balance(*args, **kwargs)
            else:
                raise RuntimeError(
                    "Bybit IP is restricted; running in simulated mode and "
                    "the requested live operation was not performed."
                ) from error
        if not isinstance(response, Mapping):
            raise RuntimeError(f"Bybit {method_name} returned a malformed response.")
        ret_code = response.get("retCode")
        if ret_code is None or str(ret_code) != "0":
            ret_message = response.get("retMsg", "missing response code")
            raise RuntimeError(
                f"Bybit {method_name} failed ({ret_code}): {ret_message}"
            )
        return dict(response)

    @staticmethod
    def _env_bool(name: str, *, default: bool) -> bool:
        value = os.getenv(name)
        if value is None:
            return default
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"Invalid boolean value for {name}: expected true or false.")

    def notify_telegram(self, message: str) -> None:
        if not self.telegram_token or not self.telegram_chat_id:
            logger.info("[NOTICE] %s", message)
            return
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        try:
            response = requests.post(
                url,
                json={"chat_id": self.telegram_chat_id, "text": message},
                timeout=(2, 5),
            )
            response.raise_for_status()
        except requests.RequestException:
            logger.exception("Telegram notification failed.")

    def notify_ntfy(self, title: str, message: str) -> None:
        if not self.ntfy_topic:
            if not self._ntfy_configuration_warned:
                logger.warning(
                    "Ntfy notifications are disabled; configure NTFY_TOPIC to enable them."
                )
                self._ntfy_configuration_warned = True
            return
        topic = quote(self.ntfy_topic, safe="-_.")
        headers = {"Title": title}
        if self.ntfy_access_token:
            headers["Authorization"] = f"Bearer {self.ntfy_access_token}"
        try:
            response = requests.post(
                f"{self.ntfy_server}/{topic}",
                data=message.encode("utf-8"),
                headers=headers,
                timeout=(2, 5),
            )
            response.raise_for_status()
        except requests.RequestException:
            logger.exception("Ntfy notification failed.")

    def _notify_portfolio_sync(self, portfolio: AvanzaPortfolioSnapshot) -> None:
        signature: tuple[object, ...] = (
            portfolio.available,
            tuple(
                sorted(
                    (
                        position.symbol,
                        position.quantity,
                        position.market_value,
                        position.currency,
                    )
                    for position in portfolio.positions
                )
            ),
            tuple(
                sorted(
                    (cash.amount, cash.currency)
                    for cash in portfolio.cash_positions
                )
            ),
        )
        if signature == self._last_portfolio_signature:
            return
        self._last_portfolio_signature = signature
        if not portfolio.available:
            self.notify_ntfy(
                "Avanza portfolio unavailable",
                "The saved positions file is missing. No Avanza cash or holdings "
                "were included in the portfolio snapshot.",
            )
            return
        cash_currencies = sorted({cash.currency for cash in portfolio.cash_positions})
        self.notify_ntfy(
            "Avanza portfolio synchronized",
            f"Portfolio snapshot updated: {len(portfolio.positions)} stock positions "
            f"and liquid cash in {', '.join(cash_currencies) or 'no currencies'}.",
        )

    def _notify_signal_changes(
        self, signals: Iterable[LiveTradeSignal]
    ) -> None:
        current_signatures: dict[str, str] = {}
        for signal in signals:
            signature = json.dumps(
                {
                    "side": signal.side,
                    "entry_price": signal.entry_price,
                    "win_probability": signal.win_probability,
                    "expected_value_pct": signal.expected_value_pct,
                    "take_profit_pct": signal.take_profit_pct,
                    "stop_loss_pct": signal.stop_loss_pct,
                    "technical_signals": dict(signal.technical_signals),
                    "predictive_parameters": signal.predictive_parameters,
                },
                sort_keys=True,
                default=str,
            )
            current_signatures[signal.symbol] = signature
            previous_signature = self._last_signal_signatures.get(signal.symbol)
            if previous_signature != signature:
                change = "changed" if previous_signature is not None else "detected"
                self.notify_ntfy(
                    f"Trading signal {change}",
                    f"{signal.symbol} {signal.side} signal at {signal.entry_price:g}; "
                    f"win probability {signal.win_probability:.1%}.",
                )
        for symbol in self._last_signal_signatures.keys() - current_signatures.keys():
            self.notify_ntfy(
                "Trading signal cleared",
                f"No active signal remains for {symbol}.",
            )
        self._last_signal_signatures = current_signatures

    def _notify_trade_closed(self, outcome: TradeOutcome) -> None:
        signal_ids = ", ".join(outcome.signal_ids)
        message = (
            f"Trade closed | signal={signal_ids} | {outcome.symbol} "
            f"{outcome.side} | size={outcome.quantity:.12g} | "
            f"entry={outcome.entry_price:.12g} | exit={outcome.exit_price:.12g} | "
            f"net={outcome.net_pnl:.8g} USDT "
            f"({outcome.net_return_pct:.2f}%)\n{outcome.analysis}"
        )
        logger.info("%s", message)
        self.notify_telegram(message)

    @staticmethod
    def _finite_number(value: object, field: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError(f"{field} must be a number.")
        try:
            number = float(value)
        except ValueError as error:
            raise ValueError(f"{field} must be a number.") from error
        if not math.isfinite(number):
            raise ValueError(f"{field} must be finite.")
        return number

    def validate_signal(self, signal: LiveTradeSignal) -> float:
        """Validate all required inputs and return independently checked EV."""
        if not isinstance(signal, LiveTradeSignal):
            raise TypeError(
                "Only fully populated LiveTradeSignal objects are accepted."
            )
        if not signal.symbol or not signal.symbol.isalnum():
            raise ValueError("Signal must contain a valid Bybit symbol.")
        if signal.category != "spot" or signal.side not in {"Buy", "Sell"}:
            raise ValueError(
                "Only explicitly qualified Bybit spot Buy/Sell signals run."
            )

        entry_price = self._finite_number(signal.entry_price, "entry_price")
        probability = self._finite_number(signal.win_probability, "win_probability")
        take_profit = self._finite_number(signal.take_profit_pct, "take_profit_pct")
        stop_loss = self._finite_number(signal.stop_loss_pct, "stop_loss_pct")
        costs = self._finite_number(signal.estimated_cost_pct, "estimated_cost_pct")
        reported_ev = self._finite_number(
            signal.expected_value_pct, "expected_value_pct"
        )
        bid_depth = self._finite_number(
            signal.orderbook_bid_depth_usdt, "orderbook_bid_depth_usdt"
        )
        ask_depth = self._finite_number(
            signal.orderbook_ask_depth_usdt, "orderbook_ask_depth_usdt"
        )
        cvd = self._finite_number(signal.cvd, "cvd")

        if entry_price <= 0 or not 0 < probability < 1:
            raise ValueError(
                "Entry price and win probability are outside valid bounds."
            )
        if probability < MIN_EXECUTION_WIN_PROBABILITY:
            raise ValueError(
                "Execution requires a calibrated win probability of at least 80%."
            )
        if self.probability_calibrator is None:
            raise ValueError("No isotonic probability calibration data is configured.")
        calibrated_probability = self.probability_calibrator.predict(
            signal.probability_features.score()
        )
        if not math.isclose(
            probability, calibrated_probability, rel_tol=1e-6, abs_tol=1e-8
        ):
            raise ValueError(
                "Signal win probability does not match its calibrated feature probability."
            )
        if (
            take_profit < self.target_tp_pct
            or stop_loss <= 0
            or stop_loss > self.target_sl_pct
            or take_profit / stop_loss < 2.0
        ):
            raise ValueError("Signal must target at least +10% TP and at most -5% SL.")
        if costs < 0:
            raise ValueError("Estimated trading costs cannot be negative.")
        if signal.expected_value_verified is not True or reported_ev <= 0:
            raise ValueError("A verified, positive expected value is required.")
        if signal.orderbook_verified is not True:
            raise ValueError("Orderbook depth must be externally verified.")
        if (
            min(bid_depth, ask_depth) <= 0
            or min(bid_depth, ask_depth) < self.min_orderbook_depth_usdt
        ):
            raise ValueError("Both orderbook sides must meet the configured depth.")
        orderbook_imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth)
        if (signal.side == "Buy" and orderbook_imbalance <= 0) or (
            signal.side == "Sell" and orderbook_imbalance >= 0
        ):
            raise ValueError("Order book imbalance must confirm the trade direction.")
        if signal.cvd_verified is not True:
            raise ValueError("CVD must be externally verified.")
        if (signal.side == "Buy" and cvd <= 0) or (signal.side == "Sell" and cvd >= 0):
            raise ValueError("CVD must confirm the proposed trade direction.")
        if not isinstance(signal.technical_signals, Mapping):
            raise ValueError("All P1-P15 technical signals must be provided.")
        if set(signal.technical_signals) != TECHNICAL_SIGNALS or any(
            signal.technical_signals[name] is not True for name in TECHNICAL_SIGNALS
        ):
            raise ValueError("Every P1-P15 signal must be present and verified true.")
        parameters = signal.predictive_parameters
        if not isinstance(parameters, Mapping):
            raise ValueError("Every named P1-P15 predictive parameter is required.")
        normalized_parameters = P1P15Converter.convert(parameters)
        self.signal_score_history.append(signal.probability_features.score())
        regime = self.regime_selector.select(
            normalized_parameters["trend_saturation_level"]
        )
        if not regime.entry_allowed:
            raise ValueError("P15 Level 3 saturation rejects new seed entries.")
        was_p14_latched = self.circuit_breaker.p14_latched
        p14_allowed = self.circuit_breaker.check_p14(
            normalized_parameters["public_reaction_speed"]
        )
        if not p14_allowed:
            if not was_p14_latched:
                self.notify_ntfy(
                    "P14 circuit breaker activated",
                    self.circuit_breaker.p14_reason
                    or "Trading is halted pending manual review.",
                )
            raise ValueError(
                f"P14 circuit breaker is active: {self.circuit_breaker.p14_reason}"
            )

        calculated_ev = (
            probability * take_profit - (1 - probability) * stop_loss - costs
        )
        if calculated_ev <= 0 or not math.isclose(
            calculated_ev, reported_ev, rel_tol=1e-6, abs_tol=1e-8
        ):
            raise ValueError(
                "Reported EV does not match a positive independently computed EV."
            )
        return calculated_ev

    def calculate_kelly_position_size(
        self,
        win_probability: float,
        available_usdt: float,
        *,
        reward_to_risk: float = 2.0,
        confluence: float = 1.0,
    ) -> float:
        """Size from free cash using fractional Kelly, without a fixed stake cap."""
        probability = self._finite_number(win_probability, "win_probability")
        balance = self._finite_number(available_usdt, "available_usdt")
        ratio = self._finite_number(reward_to_risk, "reward_to_risk")
        confidence = self._finite_number(confluence, "confluence")
        if (
            not 0 < probability < 1
            or balance <= 0
            or ratio <= 0
            or not 0 <= confidence <= 1
        ):
            raise ValueError("Kelly inputs are outside valid bounds.")
        full_kelly = (ratio * probability - (1 - probability)) / ratio
        if probability >= HIGH_CONFLUENCE_THRESHOLD and confidence >= HIGH_CONFLUENCE_THRESHOLD:
            kelly_fraction = 1.0
        elif probability >= MIN_EXECUTION_WIN_PROBABILITY:
            kelly_fraction = 0.5
        else:
            kelly_fraction = 0.25
        stake_fraction = min(max(full_kelly * kelly_fraction, 0.0), 1.0)
        return balance * stake_fraction

    @staticmethod
    def log_analytical_path_comparison(
        path_name: str, realized_yield: float, baseline_yield: float
    ) -> None:
        """Record a measured improvement without changing trading parameters."""
        candidate = HjarnanLiveEngine._finite_number(realized_yield, "realized_yield")
        baseline = HjarnanLiveEngine._finite_number(baseline_yield, "baseline_yield")
        if candidate > baseline:
            logger.info(
                "Self-optimization candidate: analytical path %s yielded %.4f "
                "versus baseline %.4f.",
                path_name,
                candidate,
                baseline,
            )

    @classmethod
    def calculate_protective_stop(
        cls,
        entry_price: float,
        peak_price: float,
        current_stop: float,
        *,
        breakeven_trigger_pct: float = 0.05,
        trailing_activation_pct: float = 0.10,
        trailing_distance_pct: float = 0.05,
    ) -> float:
        """Raise a long spot stop to breakeven, then trail the observed peak."""
        entry = cls._finite_number(entry_price, "entry_price")
        peak = cls._finite_number(peak_price, "peak_price")
        stop = cls._finite_number(current_stop, "current_stop")
        breakeven_trigger = cls._finite_number(
            breakeven_trigger_pct, "breakeven_trigger_pct"
        )
        trailing_activation = cls._finite_number(
            trailing_activation_pct, "trailing_activation_pct"
        )
        trailing_distance = cls._finite_number(
            trailing_distance_pct, "trailing_distance_pct"
        )
        if (
            entry <= 0
            or peak <= 0
            or stop <= 0
            or not 0 < breakeven_trigger < trailing_activation
            or not 0 < trailing_distance < 1
        ):
            raise ValueError("Protective-stop inputs are outside valid bounds.")
        if peak + entry * 1e-12 < entry * (1 + breakeven_trigger):
            return stop
        updated_stop = max(stop, entry)
        if peak + entry * 1e-12 >= entry * (1 + trailing_activation):
            updated_stop = max(updated_stop, peak * (1 - trailing_distance))
        return updated_stop

    @classmethod
    def update_protective_stops(
        cls,
        positions: Iterable[ManagedSpotPosition],
        stop_updater: Callable[[str, float], None],
    ) -> None:
        """Push only upward breakeven/trailing-stop adjustments to an adapter."""
        for position in positions:
            updated_stop = cls.calculate_protective_stop(
                position.entry_price,
                position.peak_price,
                position.stop_price,
            )
            if updated_stop > position.stop_price:
                stop_updater(position.symbol, updated_stop)

    def _available_balance(self, coin: str) -> float:
        response = self._bybit_request(
            "get_wallet_balance",
            accountType="UNIFIED", coin=coin
        )
        accounts = response.get("result", {}).get("list", [])
        if response.get("retCode") != 0 or not accounts:
            raise RuntimeError(f"Could not retrieve Bybit {coin} balance.")
        coins = accounts[0].get("coin", [])
        coin_data = next((item for item in coins if item.get("coin") == coin), None)
        if coin_data is None:
            return 0.0
        available = coin_data.get("availableToWithdraw")
        if available not in (None, ""):
            amount = self._finite_number(available, f"{coin} available balance")
        else:
            wallet_balance = self._finite_number(
                coin_data.get("walletBalance"), f"{coin} wallet balance"
            )
            locked = self._finite_number(coin_data.get("locked", 0), f"{coin} locked")
            amount = wallet_balance - locked
        return max(amount, 0.0)

    def get_liquid_funds_by_source(self) -> dict[str, float]:
        """Return liquid balances by venue and currency without valuing holdings."""
        portfolio = load_avanza_portfolio(self.avanza_positions_file)
        liquid_funds = {"Bybit USDT": self._available_balance("USDT")}
        for cash in portfolio.cash_positions:
            key = f"Avanza {cash.currency}"
            liquid_funds[key] = liquid_funds.get(key, 0.0) + cash.amount
        return liquid_funds

    def _account_equity(self) -> float:
        response = self._bybit_request(
            "get_wallet_balance",
            accountType="UNIFIED", coin="USDT"
        )
        accounts = response.get("result", {}).get("list", [])
        if response.get("retCode") != 0 or not accounts:
            raise RuntimeError("Could not retrieve Bybit account equity.")
        equity = self._finite_number(accounts[0].get("totalEquity"), "total equity")
        if equity <= 0:
            raise RuntimeError("Bybit account equity must be positive.")
        self.equity_history.append(equity)
        return equity

    def _check_daily_drawdown(self, equity: float) -> bool:
        today = datetime.now(timezone.utc).date()
        if self._equity_date != today:
            if self._equity_date is not None or self.daily_start_equity_usdt is None:
                self.daily_start_equity_usdt = equity
            self._equity_date = today
            self._daily_halted = False
            self._daily_trade_count = 0

        assert self.daily_start_equity_usdt is not None
        was_halted = self._daily_halted
        allowed = self.circuit_breaker.check_daily_drawdown(
            equity, self.daily_start_equity_usdt, today
        )
        self._daily_halted = not allowed
        if self._daily_halted and not was_halted:
            logger.critical(
                "Daily drawdown circuit breaker triggered: %s",
                self.circuit_breaker.drawdown_reason,
            )
            self.notify_telegram(
                f"Trading halted: {self.circuit_breaker.drawdown_reason}"
            )
        return allowed

    @staticmethod
    def _decimal(value: float) -> Decimal:
        return Decimal(str(value))

    @classmethod
    def _round_down_to_step(cls, value: float, step: str) -> Decimal:
        amount = cls._decimal(value)
        increment = Decimal(step)
        if increment <= 0:
            raise ValueError("Bybit instrument step must be positive.")
        return (amount / increment).to_integral_value(rounding=ROUND_DOWN) * increment

    def _instrument_rules(self, symbol: str) -> tuple[str, str, float, float]:
        response = self._bybit_request(
            "get_instruments_info",
            category="spot", symbol=symbol
        )
        instruments = response.get("result", {}).get("list", [])
        if response.get("retCode") != 0 or len(instruments) != 1:
            raise RuntimeError(f"Could not retrieve trading rules for {symbol}.")
        instrument = instruments[0]
        lot_filter = instrument.get("lotSizeFilter", {})
        price_filter = instrument.get("priceFilter", {})
        qty_step = lot_filter.get("qtyStep")
        min_qty = lot_filter.get("minOrderQty")
        tick_size = price_filter.get("tickSize")
        if not qty_step or not min_qty or not tick_size:
            raise RuntimeError(f"Incomplete Bybit trading rules for {symbol}.")
        raw_min_order_amount = lot_filter.get("minOrderAmt")
        min_order_amount = (
            self._finite_number(raw_min_order_amount, f"{symbol} minOrderAmt")
            if raw_min_order_amount not in (None, "")
            else 0.0
        )
        return (
            str(qty_step),
            str(tick_size),
            self._finite_number(min_qty, f"{symbol} minOrderQty"),
            min_order_amount,
        )

    def execute_bybit_limit_trade(self, signal: LiveTradeSignal) -> str | None:
        """Place one validated spot order; returns its ID or rejects it."""
        if self.mock_mode:
            raise RuntimeError("Simulated mode cannot submit Bybit orders.")
        if not self.bybit_testnet and not self.allow_mainnet_trading:
            raise RuntimeError(
                "Mainnet orders are disabled; set BYBIT_ALLOW_MAINNET_TRADING=true "
                "to explicitly opt in."
            )
        self.validate_signal(signal)
        equity = self._account_equity()
        if not self._check_daily_drawdown(equity):
            raise RuntimeError("Daily drawdown circuit breaker is active.")
        if self._daily_trade_count >= self.max_trades_per_day:
            logger.info(
                "Daily trade limit reached (%d); no additional order will be placed.",
                self.max_trades_per_day,
            )
            return None

        if not signal.symbol.endswith("USDT"):
            raise ValueError("This spot engine only supports USDT-quoted symbols.")

        base_coin = signal.symbol.removesuffix("USDT")
        if signal.side == "Buy":
            available_capital = self._available_balance("USDT")
        else:
            available_capital = self._available_balance(base_coin) * signal.entry_price
        position_value = self.calculate_kelly_position_size(
            signal.win_probability,
            available_capital,
            reward_to_risk=signal.take_profit_pct / signal.stop_loss_pct,
            confluence=sum(
                signal.technical_signals.values()
            )
            / len(TECHNICAL_SIGNALS),
        )
        if position_value <= 0:
            logger.info(
                "Kelly sizing rejected %s: calculated position is zero.", signal.symbol
            )
            return None

        qty_step, tick_size, minimum_quantity, minimum_amount = self._instrument_rules(
            signal.symbol
        )
        qty = position_value / signal.entry_price
        if signal.side == "Sell":
            qty = min(qty, self._available_balance(base_coin))
        rounded_qty = self._round_down_to_step(qty, qty_step)
        if float(rounded_qty) < minimum_quantity:
            raise ValueError(f"Calculated order is below the {signal.symbol} minimum.")

        limit_price = self._round_down_to_step(signal.entry_price, tick_size)
        if signal.side == "Buy":
            tp_price = signal.entry_price * (1 + signal.take_profit_pct)
            sl_price = signal.entry_price * (1 - signal.stop_loss_pct)
        else:
            tp_price = signal.entry_price * (1 - signal.take_profit_pct)
            sl_price = signal.entry_price * (1 + signal.stop_loss_pct)
        rounded_tp = self._round_down_to_step(tp_price, tick_size)
        rounded_sl = self._round_down_to_step(sl_price, tick_size)
        if float(rounded_qty) * float(limit_price) < minimum_amount:
            raise ValueError(f"Calculated order is below the {signal.symbol} minimum.")
        stop_distance = abs(float(rounded_sl) - float(limit_price))
        if stop_distance == 0:
            raise ValueError("Rounded stop-loss price equals the entry price.")
        actual_rrr = abs(float(rounded_tp) - float(limit_price)) / abs(stop_distance)
        actual_reward = abs(float(rounded_tp) - float(limit_price)) / float(limit_price)
        actual_risk = abs(float(rounded_sl) - float(limit_price)) / float(limit_price)
        if (
            actual_rrr < 2.0
            or actual_reward < self.target_tp_pct
            or actual_risk > self.target_sl_pct
            or rounded_sl == limit_price
        ):
            raise ValueError(
                "Rounded order prices do not preserve TP, SL, and RRR limits."
            )

        order_link_id = f"ia{uuid.uuid4().hex}"
        signal_ids = tuple(
            sorted(
                signal_id
                for signal_id, active in signal.technical_signals.items()
                if active
            )
        )
        self.trade_analyzer.register_order(
            order_link_id,
            symbol=signal.symbol,
            signal_ids=signal_ids,
            expected_value_pct=signal.expected_value_pct,
        )
        try:
            response = self._bybit_request(
                "place_order",
                category="spot",
                symbol=signal.symbol,
                side=signal.side,
                orderType="Limit",
                qty=str(rounded_qty),
                price=str(limit_price),
                timeInForce="PostOnly",
                takeProfit=str(rounded_tp),
                stopLoss=str(rounded_sl),
                orderLinkId=order_link_id,
            )
            if response.get("retCode") != 0:
                raise RuntimeError(f"Bybit rejected order: {response.get('retMsg')}")
        except Exception:
            self.trade_analyzer.unregister_order(order_link_id)
            raise
        self._daily_trade_count += 1
        order_id = response.get("result", {}).get("orderId")
        if not isinstance(order_id, str) or not order_id:
            raise RuntimeError("Bybit accepted order without returning an order ID.")
        logger.info(
            "Bybit %s %s order submitted: qty=%s entry=%s TP=%s SL=%s id=%s",
            signal.side,
            signal.symbol,
            rounded_qty,
            limit_price,
            rounded_tp,
            rounded_sl,
            order_id,
        )
        return order_id

    def track_avanza_positions(self) -> list[AvanzaPosition]:
        """Load and normalize saved Avanza positions without placing stock orders."""
        portfolio = load_avanza_portfolio(self.avanza_positions_file)
        logger.info("Tracked %d Avanza positions.", len(portfolio.positions))
        return list(portfolio.positions)

    @staticmethod
    def analyze_stock_rotation(
        positions: Iterable[AvanzaPosition],
        target_symbols: Iterable[str],
        *,
        news_impact_scores: Mapping[str, float] | None = None,
    ) -> list[RotationCandidate]:
        """Emit advisory holdings/target differences; never submits Avanza orders."""
        held = {position.symbol.upper() for position in positions}
        targets = {
            symbol.strip().upper() for symbol in target_symbols if symbol.strip()
        }
        scores = {
            symbol.strip().upper(): HjarnanLiveEngine._finite_number(
                score, "news impact score"
            )
            for symbol, score in (news_impact_scores or {}).items()
        }
        candidates = [
            RotationCandidate(
                "sell",
                symbol,
                "Holding is not in target universe.",
                scores.get(symbol, 0.0),
            )
            for symbol in sorted(held - targets)
        ]
        candidates.extend(
            RotationCandidate(
                "buy",
                symbol,
                "Target is not currently held.",
                scores.get(symbol, 0.0),
            )
            for symbol in sorted(targets - held)
        )
        for candidate in candidates:
            logger.info(
                "Avanza rotation candidate (%s): %s — %s (news impact %.2f).",
                candidate.action,
                candidate.symbol,
                candidate.reason,
                candidate.news_impact_score,
            )
        return candidates

    def monitor_avanza_portfolio(
        self, target_symbols: Iterable[str] | None = None
    ) -> list[AvanzaPosition]:
        positions = self.track_avanza_positions()
        if target_symbols is not None:
            self.analyze_stock_rotation(positions, target_symbols)
        return positions

    def process_market_cycle(
        self,
        active_positions: Iterable[dict[str, object]],
        incoming_signals: Iterable[dict[str, object]],
        *,
        liquidate_position: Callable[[dict[str, object]], object] | None = None,
        enter_signal: Callable[[dict[str, object]], object] | None = None,
    ) -> int:
        """Reallocate snapshots using explicit, caller-owned execution adapters.

        The close callback must return ``False`` when liquidation did not
        succeed. An entry callback is only called after a successful close.
        """
        liquidate_position = liquidate_position or self._liquidate_bybit_position
        enter_signal = enter_signal or self._enter_bybit_signal
        if not callable(liquidate_position) or not callable(enter_signal):
            raise TypeError("Reallocation requires callable close and entry adapters.")

        positions = list(active_positions)
        signals = list(incoming_signals)
        if any(not isinstance(position, dict) for position in positions):
            raise TypeError("Every active position must be a dict.")
        if any(not isinstance(signal, dict) for signal in signals):
            raise TypeError("Every incoming signal must be a dict.")

        used_signals: set[int] = set()
        reallocations = 0
        for position in positions:
            current_symbol = position.get("symbol")
            if not isinstance(current_symbol, str) or not current_symbol:
                raise ValueError("Every active position must include a symbol.")

            best_signal: dict[str, object] | None = None
            best_advantage = self.opportunity_optimizer.fee_hurdle
            for signal in signals:
                if id(signal) in used_signals:
                    continue
                new_symbol = signal.get("symbol")
                if not isinstance(new_symbol, str) or not new_symbol:
                    raise ValueError("Every incoming signal must include a symbol.")
                if new_symbol == current_symbol:
                    continue
                if not self.opportunity_optimizer.evaluate_reallocation(
                    position, signal
                ):
                    continue

                advantage = (
                    float(signal["new_roi"]) - float(position["current_roi"])
                )
                if best_signal is None or advantage > best_advantage:
                    best_signal = signal
                    best_advantage = advantage

            if best_signal is None:
                continue
            if liquidate_position(position) is False:
                raise RuntimeError(
                    f"Liquidation failed for {current_symbol}; re-entry was not attempted."
                )
            try:
                entry_result = enter_signal(best_signal)
            except Exception as error:
                raise RuntimeError(
                    f"Re-entry into {best_signal['symbol']} failed after "
                    f"liquidating {current_symbol}."
                ) from error
            if entry_result is False:
                raise RuntimeError(
                    f"Re-entry into {best_signal['symbol']} failed after "
                    f"liquidating {current_symbol}."
                )
            used_signals.add(id(best_signal))
            reallocations += 1

        return reallocations

    def _liquidate_bybit_position(
        self, position: dict[str, object]
    ) -> dict[str, Any]:
        if self.mock_mode or self.bybit_client is None:
            raise RuntimeError("Mock mode cannot liquidate Bybit positions.")
        symbol = position.get("symbol")
        if not isinstance(symbol, str):
            raise ValueError("Active position must include a Bybit symbol.")
        return self.bybit_client.close_position(symbol)

    def _enter_bybit_signal(self, signal: dict[str, object]) -> dict[str, Any]:
        if self.mock_mode or self.bybit_client is None:
            raise RuntimeError("Mock mode cannot enter Bybit positions.")
        live_signal = signal.get("live_trade_signal")
        if not isinstance(live_signal, LiveTradeSignal):
            raise TypeError(
                "A reallocation entry requires a validated LiveTradeSignal in "
                "'live_trade_signal'."
            )
        if signal.get("symbol") != live_signal.symbol:
            raise ValueError("Reallocation symbol does not match LiveTradeSignal.")
        self.validate_signal(live_signal)
        if not self._check_daily_drawdown(self._account_equity()):
            raise RuntimeError("Daily drawdown circuit breaker is active.")
        if self._daily_trade_count >= self.max_trades_per_day:
            raise RuntimeError("Daily trade limit has been reached.")
        raw_quantity = signal.get("qty")
        quantity = self._finite_number(raw_quantity, "reallocation quantity")
        if quantity <= 0:
            raise ValueError("Reallocation quantity must be positive.")
        if live_signal.side == "Buy":
            available = self._available_balance("USDT")
            if quantity * live_signal.entry_price > available:
                raise ValueError("Reallocation buy exceeds available USDT balance.")
        else:
            base_coin = live_signal.symbol.removesuffix("USDT")
            if quantity > self._available_balance(base_coin):
                raise ValueError("Reallocation sell exceeds available base balance.")
        result = self.bybit_client.execute_order(
            live_signal.symbol,
            live_signal.side,
            quantity,
            "Market",
        )
        self._daily_trade_count += 1
        return result

    def run_live_loop(
        self,
        signal_provider: Callable[[], Iterable[LiveTradeSignal]],
        *,
        interval_seconds: float = 10.0,
        avanza_target_symbols_provider: (
            Callable[[list[AvanzaPosition]], Iterable[str]] | None
        ) = None,
        protective_stop_provider: Callable[[], Iterable[ManagedSpotPosition]] | None = None,
        protective_stop_updater: Callable[[str, float], None] | None = None,
    ) -> None:
        """Run until stopped, the drawdown breaker trips, or an error is raised."""
        if interval_seconds <= 0 or not math.isfinite(interval_seconds):
            raise ValueError("Loop interval must be a positive finite number.")
        if (protective_stop_provider is None) != (protective_stop_updater is None):
            raise ValueError(
                "Protective-stop provider and updater must be configured together."
            )
        logger.info(
            "Live engine started (Bybit endpoint=%s; testnet=%s; "
            "preferred order cadence %d-%d/day).",
            self.bybit_endpoint,
            self.bybit_testnet,
            self.target_trades_per_day,
            self.max_trades_per_day,
        )
        startup_message = "Live trading engine started; external signals are required."
        self.notify_telegram(startup_message)
        self.notify_ntfy("Live engine started", startup_message)
        listener = self._start_execution_listener()
        try:
            while not self._daily_halted:
                try:
                    equity = self._account_equity()
                    if not self._check_daily_drawdown(equity):
                        break

                    portfolio = load_avanza_portfolio(self.avanza_positions_file)
                    positions = list(portfolio.positions)
                    self._notify_portfolio_sync(portfolio)
                    if avanza_target_symbols_provider is not None:
                        targets = avanza_target_symbols_provider(positions)
                        self.analyze_stock_rotation(positions, targets)

                    if protective_stop_provider is not None and protective_stop_updater is not None:
                        self.update_protective_stops(
                            protective_stop_provider(), protective_stop_updater
                        )

                    signals = list(signal_provider())
                    self._notify_signal_changes(signals)
                    for signal in signals:
                        if self._daily_halted:
                            break
                        self.execute_bybit_limit_trade(signal)
                    if not self._daily_halted:
                        time.sleep(interval_seconds)
                except KeyboardInterrupt:
                    logger.info("Live engine stopped by user.")
                    break
                except Exception:
                    if self._daily_halted:
                        break
                    logger.exception(
                        "Live engine cycle failed; no further signals this cycle."
                    )
                    time.sleep(max(interval_seconds, 30.0))
        finally:
            if listener is not None:
                listener.stop()

    def _start_execution_listener(self) -> ExecutionStreamListener | None:
        if self.mock_mode:
            logger.info("Skipping Bybit execution stream in mock mode.")
            return None
        credentials = self.sessions.get_bybit_credentials()
        api_key = credentials.get("api_key")
        api_secret = credentials.get("api_secret")
        testnet = credentials.get("testnet")
        if (
            not isinstance(api_key, str)
            or not isinstance(api_secret, str)
            or not isinstance(testnet, bool)
        ):
            raise RuntimeError("Bybit credentials are not valid for the execution stream.")
        domain, tld = bybit_api_domain_parts(
            os.getenv("BYBIT_DOMAIN", "bybit.com")
        )
        listener = ExecutionStreamListener(
            testnet=testnet,
            domain=domain,
            tld=tld,
            api_key=api_key,
            api_secret=api_secret,
            execution_handler=self.trade_analyzer.record_execution,
        )
        listener.start()
        return listener


if __name__ == "__main__":
    raise SystemExit(
        "No verified signal provider is configured. Import HjarnanLiveEngine "
        "and supply a validated signal provider before starting the live loop."
    )
