from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pybit.unified_trading import HTTP

LOG_FILE = "trading_bot.log"
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
POLL_SECONDS = 60
VALID_INTERVALS = {"1", "3", "5", "15", "30", "60", "120", "240", "360", "720"}
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BotConfig:
    symbols: tuple[str, ...]
    interval: str = "15"
    fast_ema: int = 9
    slow_ema: int = 21
    rsi_period: int = 14
    atr_period: int = 14
    risk_per_trade: float = 0.005
    max_leverage: float = 1.0
    stop_atr_multiple: float = 1.5
    reward_to_risk: float = 2.0
    trading_enabled: bool = False
    testnet: bool = False
    allow_mainnet_trading: bool = False
    position_mode: str = "one_way"

    @classmethod
    def from_environment(cls) -> BotConfig:
        symbols = tuple(
            symbol.strip().upper()
            for symbol in os.getenv("BOT_SYMBOLS", "BTCUSDT").split(",")
            if symbol.strip()
        )
        if not symbols or any(not symbol.isalnum() or not symbol.endswith("USDT") for symbol in symbols):
            raise ValueError(
                "BOT_SYMBOLS must be comma-separated USDT symbols, such as BTCUSDT.")
        if len(set(symbols)) != len(symbols):
            raise ValueError("BOT_SYMBOLS must not contain duplicates.")

        interval = os.getenv("BOT_INTERVAL", "15").strip()
        if interval not in VALID_INTERVALS:
            raise ValueError(
                f"BOT_INTERVAL must be one of {', '.join(sorted(VALID_INTERVALS))}.")

        fast_ema = _env_int("BOT_FAST_EMA", 9, minimum=2)
        slow_ema = _env_int("BOT_SLOW_EMA", 21, minimum=3)
        rsi_period = _env_int("BOT_RSI_PERIOD", 14, minimum=2)
        atr_period = _env_int("BOT_ATR_PERIOD", 14, minimum=2)
        if fast_ema >= slow_ema:
            raise ValueError("BOT_FAST_EMA must be smaller than BOT_SLOW_EMA.")

        risk = _env_float("BOT_RISK_PER_TRADE", 0.005,
                          minimum=0.0, maximum=0.02)
        leverage = _env_float("BOT_MAX_LEVERAGE", 1.0,
                              minimum=0.0, maximum=5.0)
        stop_multiple = _env_float(
            "BOT_STOP_ATR_MULTIPLE", 1.5, minimum=0.0, maximum=5.0)
        reward_to_risk = _env_float(
            "BOT_REWARD_TO_RISK", 2.0, minimum=0.0, maximum=10.0)
        if risk == 0 or leverage == 0 or stop_multiple == 0 or reward_to_risk == 0:
            raise ValueError(
                "Risk, leverage, stop distance, and reward/risk must be positive.")

        position_mode = os.getenv(
            "BOT_POSITION_MODE", "one_way").strip().lower()
        if position_mode not in {"one_way", "hedge"}:
            raise ValueError("BOT_POSITION_MODE must be 'one_way' or 'hedge'.")

        return cls(
            symbols=symbols,
            interval=interval,
            fast_ema=fast_ema,
            slow_ema=slow_ema,
            rsi_period=rsi_period,
            atr_period=atr_period,
            risk_per_trade=risk,
            max_leverage=leverage,
            stop_atr_multiple=stop_multiple,
            reward_to_risk=reward_to_risk,
            trading_enabled=parse_bool(
                os.getenv("BOT_TRADING_ENABLED", "false"), "BOT_TRADING_ENABLED"),
            testnet=parse_bool(
                os.getenv("BYBIT_TESTNET", "true"), "BYBIT_TESTNET"),
            allow_mainnet_trading=parse_bool(
                os.getenv("BYBIT_ALLOW_MAINNET_TRADING", "false"),
                "BYBIT_ALLOW_MAINNET_TRADING",
            ),
            position_mode=position_mode,
        )


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    candle_start_ms: int
    close: float
    fast_ema_previous: float
    slow_ema_previous: float
    fast_ema: float
    slow_ema: float
    rsi: float
    atr: float
    signal: str | None


def _env_int(name: str, default: int, *, minimum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as error:
        raise ValueError(f"{name} must be an integer.") from error
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}.")
    return value


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as error:
        raise ValueError(f"{name} must be a number.") from error
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value


def parse_bool(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean.")


def create_bybit_session(config: BotConfig) -> HTTP:
    api_key = os.getenv("BYBIT_API_KEY", "").strip()
    api_secret = (
        os.getenv("BYBIT_SECRET") or os.getenv("BYBIT_API_SECRET", "")
    ).strip()
    if not api_key or not api_secret:
        raise ValueError(
            "BYBIT_API_KEY and BYBIT_SECRET (or BYBIT_API_SECRET) must be configured.")

    from auth.sessions_manager import bybit_api_domain_parts
    from pybit.unified_trading import HTTP

    domain, tld = bybit_api_domain_parts(os.getenv("BYBIT_DOMAIN", "bybit.eu"))
    return HTTP(
        testnet=config.testnet,
        domain=domain,
        tld=tld,
        api_key=api_key,
        api_secret=api_secret,
    )


def calculate_ema(values: list[float], period: int) -> list[float | None]:
    if period < 1:
        raise ValueError("EMA period must be positive.")
    if len(values) < period:
        return [None] * len(values)
    result: list[float | None] = [None] * (period - 1)
    current = sum(values[:period]) / period
    result.append(current)
    multiplier = 2 / (period + 1)
    for value in values[period:]:
        current = (value - current) * multiplier + current
        result.append(current)
    return result


def calculate_rsi(values: list[float], period: int) -> float:
    if period < 1 or len(values) < period + 1:
        raise ValueError("Insufficient closes to calculate RSI.")
    changes = [values[index] - values[index - 1]
               for index in range(len(values) - period, len(values))]
    average_gain = sum(max(change, 0.0) for change in changes) / period
    average_loss = sum(max(-change, 0.0) for change in changes) / period
    if average_loss == 0:
        return 50.0 if average_gain == 0 else 100.0
    relative_strength = average_gain / average_loss
    return 100 - 100 / (1 + relative_strength)


def calculate_atr(candles: list[tuple[int, float, float, float, float]], period: int) -> float:
    if period < 1 or len(candles) < period + 1:
        raise ValueError("Insufficient candles to calculate ATR.")
    true_ranges = []
    for index in range(len(candles) - period, len(candles)):
        _, _, high, low, _ = candles[index]
        previous_close = candles[index - 1][4]
        true_ranges.append(
            max(high - low, abs(high - previous_close), abs(low - previous_close))
        )
    return sum(true_ranges) / period


def entry_signal(
    fast_previous: float,
    slow_previous: float,
    fast_current: float,
    slow_current: float,
    rsi: float,
) -> str | None:
    if fast_previous <= slow_previous and fast_current > slow_current and 50 <= rsi <= 70:
        return "Buy"
    if fast_previous >= slow_previous and fast_current < slow_current and 30 <= rsi <= 50:
        return "Sell"
    return None


def fetch_market_snapshot(
    session: Any,
    symbol: str,
    config: BotConfig,
    *,
    now_ms: int | None = None,
) -> MarketSnapshot:
    limit = min(1000, max(100, config.slow_ema + 3,
                config.rsi_period + 2, config.atr_period + 2))
    response = session.get_kline(
        category="linear",
        symbol=symbol,
        interval=config.interval,
        limit=limit,
    )
    _require_success(response, f"{symbol} K-line request")
    raw_candles = response.get("result", {}).get("list", [])
    current_time = int(time.time() * 1000) if now_ms is None else now_ms
    interval_ms = int(config.interval) * 60_000
    candles = []
    for row in raw_candles:
        if len(row) < 5:
            continue
        start_ms = int(row[0])
        if start_ms + interval_ms > current_time:
            continue
        values = tuple(float(value) for value in row[1:5])
        open_price, high, low, close = values
        if not all(math.isfinite(value) and value > 0 for value in values):
            continue
        if high < max(open_price, close, low) or low > min(open_price, close, high):
            continue
        candles.append((start_ms, open_price, high, low, close))
    candles.sort(key=lambda candle: candle[0])

    minimum_candles = max(config.slow_ema + 2,
                          config.rsi_period + 1, config.atr_period + 1)
    if len(candles) < minimum_candles:
        raise RuntimeError(
            f"Not enough closed {config.interval}-minute candles for {symbol} indicators.")

    closes = [candle[4] for candle in candles]
    fast = calculate_ema(closes, config.fast_ema)
    slow = calculate_ema(closes, config.slow_ema)
    fast_previous, fast_current = fast[-2:]
    slow_previous, slow_current = slow[-2:]
    if None in (fast_previous, fast_current, slow_previous, slow_current):
        raise RuntimeError(f"Could not calculate EMAs for {symbol}.")
    rsi = calculate_rsi(closes, config.rsi_period)
    atr = calculate_atr(candles, config.atr_period)
    signal = entry_signal(fast_previous, slow_previous,
                          fast_current, slow_current, rsi)
    return MarketSnapshot(
        candle_start_ms=candles[-1][0],
        close=closes[-1],
        fast_ema_previous=fast_previous,
        slow_ema_previous=slow_previous,
        fast_ema=fast_current,
        slow_ema=slow_current,
        rsi=rsi,
        atr=atr,
        signal=signal,
    )


def _require_success(response: dict[str, Any], operation: str) -> None:
    if response.get("retCode") != 0:
        raise RuntimeError(
            f"{operation} failed: {response.get('retMsg', 'unknown error')}")


def _floor_to_step(value: float, step: str) -> Decimal:
    amount = Decimal(str(value))
    increment = Decimal(step)
    if increment <= 0:
        raise ValueError("Bybit quantity and price steps must be positive.")
    return (amount / increment).to_integral_value(rounding=ROUND_DOWN) * increment


def _price_to_step(value: float, step: str, *, round_up: bool) -> Decimal:
    amount = Decimal(str(value))
    increment = Decimal(step)
    if increment <= 0:
        raise ValueError("Bybit price step must be positive.")
    rounding = ROUND_UP if round_up else ROUND_DOWN
    return (amount / increment).to_integral_value(rounding=rounding) * increment


def size_position(
    *,
    equity: float,
    available_balance: float,
    entry_price: float,
    stop_price: float,
    quantity_step: str,
    minimum_quantity: float,
    minimum_notional: float,
    risk_per_trade: float,
    max_leverage: float,
) -> Decimal:
    values = (equity, available_balance, entry_price,
              stop_price, risk_per_trade, max_leverage)
    if any(not math.isfinite(value) for value in values):
        raise ValueError("Position sizing inputs must be finite.")
    if equity <= 0 or available_balance <= 0 or entry_price <= 0 or risk_per_trade <= 0 or max_leverage <= 0:
        raise ValueError("Position sizing inputs must be positive.")
    stop_distance = abs(entry_price - stop_price)
    if stop_distance <= 0 or stop_price <= 0:
        raise ValueError("Stop-loss must be a positive distance from entry.")

    risk_quantity = equity * risk_per_trade / stop_distance
    margin_limited_quantity = available_balance * max_leverage / entry_price
    quantity = _floor_to_step(
        min(risk_quantity, margin_limited_quantity), quantity_step)
    if float(quantity) < minimum_quantity:
        raise ValueError(
            "Calculated position is below the Bybit minimum quantity.")
    if float(quantity) * entry_price < minimum_notional:
        raise ValueError(
            "Calculated position is below the Bybit minimum notional.")
    return quantity


def _account_funds(session: Any) -> tuple[float, float]:
    response = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
    _require_success(response, "Unified USDT wallet request")
    accounts = response.get("result", {}).get("list", [])
    if not accounts:
        raise RuntimeError("Bybit returned no unified wallet account.")
    account = accounts[0]
    equity = float(account.get("totalEquity") or 0)
    coin = next(
        (item for item in account.get("coin", []) if item.get("coin") == "USDT"),
        {},
    )
    available_value = coin.get("availableToWithdraw")
    if available_value not in (None, ""):
        available = float(available_value)
    else:
        available = float(coin.get("walletBalance") or 0) - \
            float(coin.get("locked") or 0)
    if not math.isfinite(equity) or not math.isfinite(available) or equity <= 0 or available <= 0:
        raise RuntimeError(
            "Bybit returned invalid or unavailable USDT equity.")
    return equity, available


def _instrument_rules(session: Any, symbol: str) -> tuple[str, str, float, float]:
    response = session.get_instruments_info(category="linear", symbol=symbol)
    _require_success(response, f"{symbol} instrument-info request")
    instruments = response.get("result", {}).get("list", [])
    if len(instruments) != 1:
        raise RuntimeError(
            f"Could not retrieve Bybit trading rules for {symbol}.")
    instrument = instruments[0]
    lot_filter = instrument.get("lotSizeFilter", {})
    price_filter = instrument.get("priceFilter", {})
    quantity_step = lot_filter.get("qtyStep")
    tick_size = price_filter.get("tickSize")
    minimum_quantity = lot_filter.get("minOrderQty")
    minimum_notional = lot_filter.get(
        "minNotionalValue") or lot_filter.get("minOrderAmt") or 0
    if not quantity_step or not tick_size or minimum_quantity in (None, ""):
        raise RuntimeError(f"Incomplete Bybit trading rules for {symbol}.")
    return str(quantity_step), str(tick_size), float(minimum_quantity), float(minimum_notional)


def _position_index(config: BotConfig, side: str) -> int:
    if config.position_mode == "one_way":
        return 0
    return 1 if side == "Buy" else 2


def _active_positions(session: Any, symbol: str) -> list[dict[str, Any]]:
    response = session.get_positions(category="linear", symbol=symbol)
    _require_success(response, f"{symbol} positions request")
    positions = response.get("result", {}).get("list", [])
    active = []
    for position in positions:
        size = float(position.get("size") or 0)
        side = position.get("side")
        if size > 0 and side in {"Buy", "Sell"}:
            active.append(position)
    return active


def _close_position(session: Any, symbol: str, position: dict[str, Any]) -> None:
    side = "Sell" if position["side"] == "Buy" else "Buy"
    response = session.place_order(
        category="linear",
        symbol=symbol,
        side=side,
        orderType="Market",
        qty=str(position["size"]),
        positionIdx=int(position.get("positionIdx", 0)),
        reduceOnly=True,
    )
    _require_success(response, f"{symbol} reduce-only close")
    order_id = response.get("result", {}).get("orderId", "unknown")
    logger.info(
        "Exit order accepted: %s %s qty=%s order_id=%s",
        side,
        symbol,
        position["size"],
        order_id,
    )


def process_market_check(
    session: Any,
    symbol: str,
    config: BotConfig,
    processed_signals: dict[str, tuple[int, str]],
    pending_closes: set[tuple[str, int]],
) -> None:
    market = fetch_market_snapshot(session, symbol, config)
    logger.info(
        "Market check %s close=%.8g EMA%d=%.8g EMA%d=%.8g RSI%d=%.2f ATR%d=%.8g signal=%s",
        symbol,
        market.close,
        config.fast_ema,
        market.fast_ema,
        config.slow_ema,
        market.slow_ema,
        config.rsi_period,
        market.rsi,
        config.atr_period,
        market.atr,
        market.signal or "none",
    )
    if market.signal is None:
        return

    logger.info(
        "Active signal: %s %s candle_start_ms=%d",
        market.signal.upper(),
        symbol,
        market.candle_start_ms,
    )
    signal_key = (market.candle_start_ms, market.signal)
    if processed_signals.get(symbol) == signal_key:
        logger.info("Signal already processed for %s candle %d.",
                    symbol, market.candle_start_ms)
        return

    positions = _active_positions(session, symbol)
    current_indices = {int(position.get("positionIdx", 0))
                       for position in positions}
    pending_closes.difference_update(
        key for key in pending_closes if key[0] == symbol and key[1] not in current_indices
    )
    if any(position["side"] == market.signal for position in positions):
        processed_signals[symbol] = signal_key
        logger.info("Signal skipped: %s %s position is already open.",
                    market.signal, symbol)
        return

    if positions:
        processed_signals[symbol] = signal_key
        for position in positions:
            position_index = int(position.get("positionIdx", 0))
            pending_key = (symbol, position_index)
            if pending_key in pending_closes:
                logger.info(
                    "Close already pending for %s positionIdx=%d.", symbol, position_index)
                continue
            _close_position(session, symbol, position)
            pending_closes.add(pending_key)
        return

    processed_signals[symbol] = signal_key
    if not config.trading_enabled:
        logger.warning(
            "Trade signal not executed: BOT_TRADING_ENABLED is false.")
        return
    if not config.testnet and not config.allow_mainnet_trading:
        logger.error(
            "Trade signal not executed on mainnet: explicitly set "
            "BYBIT_ALLOW_MAINNET_TRADING=true to allow live orders."
        )
        return
    if market.atr <= 0 or not math.isfinite(market.atr):
        logger.warning("Trade signal rejected for %s: ATR is invalid.", symbol)
        return

    stop_distance = market.atr * config.stop_atr_multiple
    if market.signal == "Buy":
        raw_stop = market.close - stop_distance
        raw_target = market.close + stop_distance * config.reward_to_risk
        stop_round_up = False
        target_round_up = True
    else:
        raw_stop = market.close + stop_distance
        raw_target = market.close - stop_distance * config.reward_to_risk
        stop_round_up = True
        target_round_up = False
    if raw_stop <= 0 or raw_target <= 0:
        logger.warning(
            "Trade signal rejected for %s: calculated SL/TP is not positive.", symbol)
        return

    equity, available = _account_funds(session)
    quantity_step, tick_size, minimum_quantity, minimum_notional = _instrument_rules(
        session, symbol)
    stop_price = _price_to_step(raw_stop, tick_size, round_up=stop_round_up)
    target_price = _price_to_step(
        raw_target, tick_size, round_up=target_round_up)
    quantity = size_position(
        equity=equity,
        available_balance=available,
        entry_price=market.close,
        stop_price=float(stop_price),
        quantity_step=quantity_step,
        minimum_quantity=minimum_quantity,
        minimum_notional=minimum_notional,
        risk_per_trade=config.risk_per_trade,
        max_leverage=config.max_leverage,
    )

    order = session.place_order(
        category="linear",
        symbol=symbol,
        side=market.signal,
        orderType="Market",
        qty=format(quantity, "f"),
        positionIdx=_position_index(config, market.signal),
        tpslMode="Full",
        takeProfit=format(target_price, "f"),
        stopLoss=format(stop_price, "f"),
        tpTriggerBy="MarkPrice",
        slTriggerBy="MarkPrice",
        tpOrderType="Market",
        slOrderType="Market",
    )
    _require_success(order, f"{symbol} entry order")
    order_id = order.get("result", {}).get("orderId", "unknown")
    logger.info(
        "Entry order accepted: %s %s qty=%s reference=%.8g SL=%s TP=%s order_id=%s risk_usdt=%.8g",
        market.signal,
        symbol,
        quantity,
        market.close,
        stop_price,
        target_price,
        order_id,
        equity * config.risk_per_trade,
    )


def _execution_ids(response: dict[str, Any]) -> set[str]:
    _require_success(response, "Bybit execution-history request")
    return {
        str(execution["execId"])
        for execution in response.get("result", {}).get("list", [])
        if execution.get("execId")
    }


def initialize_execution_history(session: Any, symbols: tuple[str, ...]) -> dict[str, set[str]]:
    known: dict[str, set[str]] = {}
    for symbol in symbols:
        try:
            response = session.get_executions(
                category="linear", symbol=symbol, limit=50)
            known[symbol] = _execution_ids(response)
        except Exception:
            logger.exception(
                "Could not initialize execution history for %s.", symbol)
            known[symbol] = set()
    return known


def log_new_executions(session: Any, symbol: str, known_ids: set[str]) -> None:
    response = session.get_executions(
        category="linear", symbol=symbol, limit=50)
    _require_success(response, f"{symbol} execution-history request")
    executions = response.get("result", {}).get("list", [])
    for execution in reversed(executions):
        execution_id = str(execution.get("execId", ""))
        if not execution_id or execution_id in known_ids:
            continue
        known_ids.add(execution_id)
        logger.info(
            "Execution: %s %s qty=%s price=%s fee=%s order_id=%s exec_id=%s",
            execution.get("side", "unknown"),
            execution.get("symbol", symbol),
            execution.get("execQty", "unknown"),
            execution.get("execPrice", "unknown"),
            execution.get("execFee", "unknown"),
            execution.get("orderId", "unknown"),
            execution_id,
        )


def main() -> None:
    logging.basicConfig(filename=LOG_FILE,
                        level=logging.INFO, format=LOG_FORMAT)
    try:
        from dotenv import load_dotenv

        load_dotenv()
        config = BotConfig.from_environment()
        session = create_bybit_session(config)
    except Exception:
        logger.exception("Could not initialize the automated trading worker.")
        raise

    live_orders_allowed = config.trading_enabled and (
        config.testnet or config.allow_mainnet_trading
    )
    logger.info(
        "Strategy worker started: symbols=%s interval=%sm EMA=%d/%d RSI=%d ATR=%d "
        "risk=%.3f%% max_leverage=%.2fx order_execution=%s",
        ",".join(config.symbols),
        config.interval,
        config.fast_ema,
        config.slow_ema,
        config.rsi_period,
        config.atr_period,
        config.risk_per_trade * 100,
        config.max_leverage,
        "enabled" if live_orders_allowed else "disabled",
    )
    known_execution_ids = initialize_execution_history(session, config.symbols)
    processed_signals: dict[str, tuple[int, str]] = {}
    pending_closes: set[tuple[str, int]] = set()

    while True:
        for symbol in config.symbols:
            try:
                process_market_check(
                    session,
                    symbol,
                    config,
                    processed_signals,
                    pending_closes,
                )
            except Exception:
                logger.exception(
                    "Market check or order processing failed for %s.", symbol)
            try:
                log_new_executions(
                    session, symbol, known_execution_ids[symbol])
            except Exception:
                logger.exception("Could not poll executions for %s.", symbol)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
