from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import TYPE_CHECKING, Any, Final

from engine_live import (
    AvanzaPortfolioSnapshot,
    HjarnanLiveEngine,
    PREDICTIVE_PARAMETER_NAMES,
    is_bybit_ip_restriction,
    load_avanza_portfolio,
)
from auth.sessions_manager import bybit_api_domain_parts

if TYPE_CHECKING:
    from pybit.unified_trading import HTTP

LOG_FILE = "trading_bot.log"
LOG_FORMAT = "%(asctime)s | %(levelname)s | %(message)s"
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
    api_key: str = ""
    api_secret: str = ""

    @classmethod
    def from_environment(cls) -> BotConfig:
        symbols = tuple(
            symbol.strip().upper()
            for symbol in os.getenv("BOT_SYMBOLS", "BTCUSDT").split(",")
            if symbol.strip()
        )
        if not symbols or any(not symbol.isalnum() or not symbol.endswith("USDT") for symbol in symbols):
            raise ValueError("BOT_SYMBOLS måste vara USDT-symboler separerade med komma.")
        if len(set(symbols)) != len(symbols):
            raise ValueError("BOT_SYMBOLS får inte innehålla dubbletter.")

        interval = os.getenv("BOT_INTERVAL", "15").strip()
        if interval not in VALID_INTERVALS:
            raise ValueError(f"BOT_INTERVAL ogiltigt.")

        return cls(
            symbols=symbols,
            interval=interval,
            fast_ema=_env_int("BOT_FAST_EMA", 9, minimum=2),
            slow_ema=_env_int("BOT_SLOW_EMA", 21, minimum=3),
            rsi_period=_env_int("BOT_RSI_PERIOD", 14, minimum=2),
            atr_period=_env_int("BOT_ATR_PERIOD", 14, minimum=2),
            risk_per_trade=_env_float("BOT_RISK_PER_TRADE", 0.005, minimum=0.0, maximum=0.02),
            max_leverage=_env_float("BOT_MAX_LEVERAGE", 1.0, minimum=0.0, maximum=5.0),
            stop_atr_multiple=_env_float("BOT_STOP_ATR_MULTIPLE", 1.5, minimum=0.0, maximum=5.0),
            reward_to_risk=_env_float("BOT_REWARD_TO_RISK", 2.0, minimum=0.0, maximum=10.0),
            trading_enabled=parse_bool(os.getenv("BOT_TRADING_ENABLED", "false"), "BOT_TRADING_ENABLED"),
            testnet=parse_bool(os.getenv("BYBIT_TESTNET", "true"), "BYBIT_TESTNET"),
            allow_mainnet_trading=parse_bool(os.getenv("BYBIT_ALLOW_MAINNET_TRADING", "false"), "BYBIT_ALLOW_MAINNET_TRADING"),
            position_mode=os.getenv("BOT_POSITION_MODE", "one_way").strip().lower(),
            api_key=os.getenv("BYBIT_API_KEY", "").strip(),
            api_secret=os.getenv("BYBIT_API_SECRET", "").strip(),
        )


@dataclass(frozen=True, slots=True)
class RiskProfile:
    phase: str
    max_leverage: Decimal
    stop_loss_fraction: Decimal
    supported_symbols: frozenset[str]
    spot_only: bool


class RiskPolicy:
    _PHASE_1 = RiskProfile("phase_1", Decimal("5"), Decimal("0.012"), frozenset({"PEPEUSDT", "BTCUSDT"}), False)
    _PHASE_2 = RiskProfile("phase_2", Decimal("3"), Decimal("0.04"), frozenset({"BTCUSDT", "ETHUSDT"}), False)
    _PHASE_3 = RiskProfile("phase_3", Decimal("1"), Decimal("0"), frozenset({"BTCUSDT", "ETHUSDT"}), True)

    def profile_for(self, portfolio_value: Decimal, symbol: str) -> RiskProfile:
        if portfolio_value < Decimal("50000"):
            profile = self._PHASE_1
        elif portfolio_value <= Decimal("300000"):
            profile = self._PHASE_2
        else:
            profile = self._PHASE_3
        if symbol.upper().strip() not in profile.supported_symbols:
            raise ValueError(f"Symbolen tillåts inte i aktuell riskfas.")
        return profile


class ATRPositionSizer:
    def __init__(self, risk_fraction: Decimal = Decimal("0.005"), atr_multiplier: Decimal = Decimal("1.5")) -> None:
        self.risk_fraction = risk_fraction
        self.atr_multiplier = atr_multiplier

    def size_position(self, *, profile: RiskProfile, capital: Decimal, entry: Decimal, atr: Decimal) -> Decimal:
        risk_budget = capital * self.risk_fraction
        stop_distance = max(entry * profile.stop_loss_fraction, atr * self.atr_multiplier)
        if stop_distance <= 0:
            return Decimal("0")
        risk_limited = risk_budget / stop_distance
        leverage_limited = (capital * profile.max_leverage) / entry
        return min(risk_limited, leverage_limited)


class MarketGuardrails:
    def __init__(self, max_spread: Decimal = Decimal("0.0005")) -> None:
        self.max_spread = max_spread

    def validate(self, bid: Decimal, ask: Decimal) -> None:
        if ask < bid or bid <= 0:
            raise RuntimeError("Ogiltig orderbok.")
        midpoint = (bid + ask) / Decimal("2")
        spread = (ask - bid) / midpoint
        if spread > self.max_spread:
            raise RuntimeError("Spread överstiger tillåten gräns.")


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    candle_start_ms: int
    close: float
    fast_ema: float
    slow_ema: float
    rsi: float
    atr: float
    signal: str | None


def _env_int(name: str, default: int, *, minimum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as error:
        raise ValueError(f"{name} måste vara ett heltal.") from error
    return max(minimum, value)


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as error:
        raise ValueError(f"{name} måste vara ett nummer.") from error
    return max(minimum, min(maximum, value))


def parse_bool(value: str, name: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}



def calculate_rsi(values: list[float], period: int) -> float:
    if len(values) < period + 1:
        return 50.0
    changes = [values[i] - values[i - 1] for i in range(len(values) - period, len(values))]
    gain = sum(max(c, 0.0) for c in changes) / period
    loss = sum(max(-c, 0.0) for c in changes) / period
    if loss == 0:
        return 100.0 if gain > 0 else 50.0
    return 100 - (100 / (1 + (gain / loss)))


def calculate_atr(candles: list[tuple[int, float, float, float, float]], period: int) -> float:
    if len(candles) < period + 1:
        return 0.0
    ranges = []
    for i in range(len(candles) - period, len(candles)):
        _, _, high, low, _ = candles[i]
        prev_close = candles[i - 1][4]
        ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(ranges) / period


def entry_signal(fast_prev: float, slow_prev: float, fast_curr: float, slow_curr: float, rsi: float) -> str | None:
    if fast_prev <= slow_prev and fast_curr > slow_curr and 50 <= rsi <= 70:
        return "Buy"
    if fast_prev >= slow_prev and fast_curr < slow_curr and 30 <= rsi <= 50:
        return "Sell"
    return None


def fetch_market_snapshot(session: Any, symbol: str, config: BotConfig) -> MarketSnapshot:
    response = session.get_kline(category="linear", symbol=symbol, interval=config.interval, limit=100)
    if response.get("retCode") != 0:
        raise RuntimeError("Kunde inte hämta k-lines från Bybit.")
    raw = response.get("result", {}).get("list", [])
    candles = [(int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4])) for row in raw if len(row) >= 5]
    candles.sort(key=lambda x: x[0])

    closes = [c[4] for c in candles]
    fast = calculate_ema(closes, config.fast_ema)
    slow = calculate_ema(closes, config.slow_ema)
    if not fast or not slow or fast[-1] is None or slow[-1] is None:
        raise RuntimeError("Beräkning av EMA misslyckades.")

    rsi = calculate_rsi(closes, config.rsi_period)
    atr = calculate_atr(candles, config.atr_period)
    signal = entry_signal(fast[-2], slow[-2], fast[-1], slow[-1], rsi)

    return MarketSnapshot(
        candle_start_ms=candles[-1][0],
        close=closes[-1],
        fast_ema=fast[-1],
        slow_ema=slow[-1],
        rsi=rsi,
        atr=atr,
        signal=signal,
    )


def main() -> None:
    logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format=LOG_FORMAT)
    config = BotConfig.from_environment()
    session = create_bybit_session(config)
    risk_policy = RiskPolicy()
    sizer = ATRPositionSizer(Decimal(str(config.risk_per_trade)), Decimal(str(config.stop_atr_multiple)))
    guardrails = MarketGuardrails()

    logger.info("Botten startad med dynamisk risklogik och Cloudflare-proxy.")

    while True:
        for symbol in config.symbols:
            try:
                market = fetch_market_snapshot(session, symbol, config)
                if market.signal:
                    logger.info("Signal detekterad: %s för %s", market.signal, symbol)
            except Exception:
                logger.exception("Fel vid marknadskontroll för %s", symbol)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()

def create_bybit_session(config: BotConfig) -> HTTP:
    from pybit.unified_trading import HTTP

    api_key = config.api_key or os.getenv("BYBIT_API_KEY", "").strip()
    api_secret = (
        config.api_secret
        or os.getenv("BYBIT_SECRET", "").strip()
        or os.getenv("BYBIT_API_SECRET", "").strip()
    )
    if not api_key or not api_secret:
        raise ValueError("BYBIT_API_KEY and BYBIT_API_SECRET must be configured.")

    domain, tld = bybit_api_domain_parts(os.getenv("BYBIT_DOMAIN", "bybit.eu"))
    proxy_url = os.getenv("CLOUDFLARE_PROXY_URL")
    if proxy_url:
        os.environ["HTTP_PROXY"] = proxy_url
        os.environ["HTTPS_PROXY"] = proxy_url
    return HTTP(
        testnet=config.testnet,
        domain=domain,
        tld=tld,
        api_key=api_key,
        api_secret=api_secret,
    )
