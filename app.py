"""IMMENSITY AURORA Engine V5.0 risk and accounting simulation.

This module provides offline portfolio accounting, risk sizing, market-data
guardrails, and strategy selection. It does not connect to Bybit or submit
orders. The monthly trajectory in ``main`` is illustrative, not a forecast.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Final, Iterable


DecimalInput = Decimal | int | float | str

INITIAL_DEPOSIT_SEK: Final[Decimal] = Decimal("1000")
SCHEDULED_DEPOSITS_SEK: Final[dict[int, Decimal]] = {
    month: Decimal("4150") for month in range(2, 7)
}
TOTAL_SCHEDULED_DEPOSITS_SEK: Final[Decimal] = sum(
    SCHEDULED_DEPOSITS_SEK.values(), Decimal("0")
)
SPREAD_LIMIT: Final[Decimal] = Decimal("0.0005")
DEFAULT_RISK_FRACTION: Final[Decimal] = Decimal("0.005")
DEFAULT_ATR_MULTIPLIER: Final[Decimal] = Decimal("2")


class ExecutionGuardError(RuntimeError):
    """Raised when market conditions fail an execution safety check."""


class InsufficientFundsError(RuntimeError):
    """Raised when a requested ledger transfer exceeds exchange capital."""


class StrategyMode(str, Enum):
    """Available strategy decisions; neither mode submits an order."""

    DIRECTIONAL = "directional"
    FUNDING_ARBITRAGE = "funding_arbitrage"


class Direction(str, Enum):
    """Normalized directional signal for a timeframe."""

    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


class RiskPhase(str, Enum):
    """Portfolio risk tiers."""

    PHASE_1 = "phase_1"
    PHASE_2 = "phase_2"
    PHASE_3 = "phase_3"


@dataclass(frozen=True, slots=True)
class SweepMilestone:
    """A portfolio-value threshold and its fixed cold-storage transfer."""

    threshold_sek: Decimal
    amount_sek: Decimal

    def __post_init__(self) -> None:
        if self.threshold_sek <= 0 or self.amount_sek <= 0:
            raise ValueError("Sweep thresholds and amounts must be positive.")


SWEEP_MILESTONES: Final[tuple[SweepMilestone, ...]] = (
    SweepMilestone(Decimal("55000"), Decimal("5000")),
    SweepMilestone(Decimal("60000"), Decimal("10000")),
    SweepMilestone(Decimal("70000"), Decimal("10000")),
    SweepMilestone(Decimal("80000"), Decimal("10000")),
    SweepMilestone(Decimal("90000"), Decimal("10000")),
    SweepMilestone(Decimal("100000"), Decimal("30000")),
)


@dataclass(frozen=True, slots=True)
class Sweep:
    """Record of one completed internal transfer."""

    threshold_sek: Decimal
    amount_sek: Decimal


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    """Immutable view of the portfolio's separated accounting balances."""

    external_deposits_sek: Decimal
    active_exchange_sek: Decimal
    cold_storage_sek: Decimal
    total_portfolio_value_sek: Decimal
    market_generated_profit_sek: Decimal
    triggered_milestones_sek: frozenset[Decimal]


def _as_decimal(value: DecimalInput, field_name: str) -> Decimal:
    """Convert a numeric input to a finite Decimal or raise a clear error."""
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite number.")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field_name} must be a finite number.") from error
    if not result.is_finite():
        raise ValueError(f"{field_name} must be a finite number.")
    return result


class AccountLedger:
    """Track deposits, exchange capital, secured capital, and market PnL."""

    def __init__(
        self,
        initial_deposit_sek: DecimalInput = INITIAL_DEPOSIT_SEK,
        milestones: Iterable[SweepMilestone] = SWEEP_MILESTONES,
    ) -> None:
        """Create a ledger and record its initial external deposit."""
        self._external_deposits_sek = Decimal("0")
        self._active_exchange_sek = Decimal("0")
        self._cold_storage_sek = Decimal("0")
        self._triggered_milestones: set[Decimal] = set()
        self._deposit_references: set[str] = set()
        self._milestones = tuple(sorted(milestones, key=lambda item: item.threshold_sek))
        thresholds = [milestone.threshold_sek for milestone in self._milestones]
        if len(thresholds) != len(set(thresholds)):
            raise ValueError("Sweep milestone thresholds must be unique.")
        initial_amount = _as_decimal(initial_deposit_sek, "initial_deposit_sek")
        if initial_amount < 0:
            raise ValueError("initial_deposit_sek must be non-negative.")
        if initial_amount:
            self.record_external_deposit(initial_amount, reference="initial")

    @property
    def total_portfolio_value_sek(self) -> Decimal:
        """Return exchange capital plus cold-storage capital."""
        return self._active_exchange_sek + self._cold_storage_sek

    def record_external_deposit(
        self, amount_sek: DecimalInput, *, reference: str
    ) -> None:
        """Add a uniquely identified outside deposit to exchange capital."""
        amount = _as_decimal(amount_sek, "amount_sek")
        if amount <= 0:
            raise ValueError("External deposits must be positive.")
        if not reference.strip():
            raise ValueError("Deposit reference must not be empty.")
        if reference in self._deposit_references:
            raise ValueError(f"Duplicate deposit reference: {reference}.")
        self._deposit_references.add(reference)
        self._external_deposits_sek += amount
        self._active_exchange_sek += amount

    def apply_market_pnl(self, pnl_sek: DecimalInput) -> None:
        """Apply market-generated profit or loss without changing deposits."""
        pnl = _as_decimal(pnl_sek, "pnl_sek")
        self._active_exchange_sek += pnl

    def sweep_eligible_milestones(self) -> tuple[Sweep, ...]:
        """Transfer each newly reached fixed milestone exactly once.

        All due transfers are checked before any balance is changed, so an
        insufficient exchange balance cannot leave a partial sweep batch.
        """
        eligible = [
            milestone
            for milestone in self._milestones
            if milestone.threshold_sek <= self.total_portfolio_value_sek
            and milestone.threshold_sek not in self._triggered_milestones
        ]
        required = sum((item.amount_sek for item in eligible), Decimal("0"))
        if eligible and required > self._active_exchange_sek:
            raise InsufficientFundsError(
                "Eligible cold-storage sweeps exceed active exchange capital."
            )

        completed: list[Sweep] = []
        for milestone in eligible:
            self._active_exchange_sek -= milestone.amount_sek
            self._cold_storage_sek += milestone.amount_sek
            self._triggered_milestones.add(milestone.threshold_sek)
            completed.append(Sweep(milestone.threshold_sek, milestone.amount_sek))
        return tuple(completed)

    def snapshot(self) -> LedgerSnapshot:
        """Return a consistent, immutable portfolio ledger snapshot."""
        total_value = self.total_portfolio_value_sek
        return LedgerSnapshot(
            external_deposits_sek=self._external_deposits_sek,
            active_exchange_sek=self._active_exchange_sek,
            cold_storage_sek=self._cold_storage_sek,
            total_portfolio_value_sek=total_value,
            market_generated_profit_sek=total_value - self._external_deposits_sek,
            triggered_milestones_sek=frozenset(self._triggered_milestones),
        )

@dataclass(frozen=True, slots=True)
class RiskProfile:
    """Leverage, asset, and stop-loss limits for a portfolio tier."""

    phase: RiskPhase
    max_leverage: Decimal
    stop_loss_fraction: Decimal
    supported_symbols: frozenset[str]
    spot_only: bool


class RiskPolicy:
    """Select the configured risk tier from total portfolio value."""

    _PHASE_1 = RiskProfile(
        RiskPhase.PHASE_1,
        Decimal("5"),
        Decimal("0.012"),
        frozenset({"PEPEUSDT", "BTCUSDT"}),
        False,
    )
    _PHASE_2 = RiskProfile(
        RiskPhase.PHASE_2,
        Decimal("3"),
        Decimal("0.04"),
        frozenset({"BTCUSDT", "ETHUSDT"}),
        False,
    )
    _PHASE_3 = RiskProfile(
        RiskPhase.PHASE_3,
        Decimal("1"),
        Decimal("0"),
        frozenset({"BTCUSDT", "ETHUSDT"}),
        True,
    )

    def profile_for(self, portfolio_value_sek: DecimalInput, symbol: str) -> RiskProfile:
        """Return the tier for an asset, rejecting symbols outside its allowlist."""
        value = _as_decimal(portfolio_value_sek, "portfolio_value_sek")
        if value < 0:
            raise ValueError("portfolio_value_sek must be non-negative.")
        normalized_symbol = symbol.upper().strip()
        if value < Decimal("50000"):
            profile = self._PHASE_1
        elif value <= Decimal("300000"):
            profile = self._PHASE_2
        else:
            profile = self._PHASE_3
        if normalized_symbol not in profile.supported_symbols:
            raise ValueError(
                f"{normalized_symbol or 'Symbol'} is not allowed in "
                f"{profile.phase.value}."
            )
        return profile


@dataclass(frozen=True, slots=True)
class PositionSize:
    """Risk-capped order size; all quote-currency values are denominated in USDT."""

    quantity: Decimal
    notional_usdt: Decimal
    planned_risk_sek: Decimal
    risk_budget_sek: Decimal
    stop_distance_usdt: Decimal


class ATRPositionSizer:
    """Size positions to a constant SEK risk budget using ATR and stop limits."""

    def __init__(
        self,
        risk_fraction: DecimalInput = DEFAULT_RISK_FRACTION,
        atr_multiplier: DecimalInput = DEFAULT_ATR_MULTIPLIER,
    ) -> None:
        """Configure the per-trade equity risk fraction and ATR distance."""
        self.risk_fraction = _as_decimal(risk_fraction, "risk_fraction")
        self.atr_multiplier = _as_decimal(atr_multiplier, "atr_multiplier")
        if not Decimal("0") < self.risk_fraction <= Decimal("1"):
            raise ValueError("risk_fraction must be in the interval (0, 1].")
        if self.atr_multiplier <= 0:
            raise ValueError("atr_multiplier must be positive.")

    def size_position(
        self,
        *,
        profile: RiskProfile,
        active_capital_sek: DecimalInput,
        entry_price_usdt: DecimalInput,
        atr_usdt: DecimalInput,
        sek_per_usdt: DecimalInput,
    ) -> PositionSize:
        """Return an ATR-sized position capped by equity and tier leverage.

        The sizing distance is at least the configured hard-stop distance and
        at least ``atr_multiplier * ATR``. SEK risk is converted to USDT with
        the explicitly supplied SEK-per-USDT rate.
        """
        capital = _as_decimal(active_capital_sek, "active_capital_sek")
        entry = _as_decimal(entry_price_usdt, "entry_price_usdt")
        atr = _as_decimal(atr_usdt, "atr_usdt")
        fx_rate = _as_decimal(sek_per_usdt, "sek_per_usdt")
        if capital <= 0 or entry <= 0 or atr <= 0 or fx_rate <= 0:
            raise ValueError("Capital, entry price, ATR, and FX rate must be positive.")

        risk_budget_sek = capital * self.risk_fraction
        stop_distance_usdt = max(
            entry * profile.stop_loss_fraction,
            atr * self.atr_multiplier,
        )
        risk_budget_usdt = risk_budget_sek / fx_rate
        risk_limited_quantity = risk_budget_usdt / stop_distance_usdt
        leverage_limited_quantity = (
            capital * profile.max_leverage / fx_rate / entry
        )
        quantity = min(risk_limited_quantity, leverage_limited_quantity)
        notional_usdt = quantity * entry
        planned_risk_sek = quantity * stop_distance_usdt * fx_rate
        return PositionSize(
            quantity=quantity,
            notional_usdt=notional_usdt,
            planned_risk_sek=planned_risk_sek,
            risk_budget_sek=risk_budget_sek,
            stop_distance_usdt=stop_distance_usdt,
        )


@dataclass(frozen=True, slots=True)
class MarketQuality:
    """Validated top-of-book spread and estimated execution slippage."""

    spread_fraction: Decimal
    estimated_slippage_fraction: Decimal


class MarketGuardrails:
    """Reject poor or incomplete orderbook and slippage observations."""

    def __init__(self, max_spread_fraction: DecimalInput = SPREAD_LIMIT) -> None:
        """Set the maximum allowed spread and estimated slippage."""
        self.max_spread_fraction = _as_decimal(
            max_spread_fraction, "max_spread_fraction"
        )
        if self.max_spread_fraction <= 0:
            raise ValueError("max_spread_fraction must be positive.")

    def validate_orderbook(
        self,
        *,
        best_bid_usdt: DecimalInput,
        best_ask_usdt: DecimalInput,
        estimated_slippage_fraction: DecimalInput = Decimal("0"),
    ) -> MarketQuality:
        """Validate prices and abort when spread or slippage exceeds the limit."""
        bid = _as_decimal(best_bid_usdt, "best_bid_usdt")
        ask = _as_decimal(best_ask_usdt, "best_ask_usdt")
        slippage = _as_decimal(
            estimated_slippage_fraction, "estimated_slippage_fraction"
        )
        if bid <= 0 or ask <= 0 or ask < bid:
            raise ExecutionGuardError("Orderbook prices are invalid or crossed.")
        if slippage < 0:
            raise ValueError("estimated_slippage_fraction must be non-negative.")
        midpoint = (bid + ask) / Decimal("2")
        spread = (ask - bid) / midpoint
        if spread > self.max_spread_fraction:
            raise ExecutionGuardError(
                f"Orderbook spread {spread:.6%} exceeds "
                f"{self.max_spread_fraction:.4%}."
            )
        if slippage > self.max_spread_fraction:
            raise ExecutionGuardError(
                f"Estimated slippage {slippage:.6%} exceeds "
                f"{self.max_spread_fraction:.4%}."
            )
        return MarketQuality(spread, slippage)


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    """A strategy selection and whether its supplied inputs pass basic gating."""

    mode: StrategyMode
    direction: Direction | None
    eligible: bool
    reason: str


class MultiTimeframeConfluence:
    """Require full 1m, 15m, and 1h directional agreement."""

    REQUIRED_TIMEFRAMES: Final[tuple[str, ...]] = ("1m", "15m", "1h")

    def select(
        self,
        signals: dict[str, Direction],
        *,
        funding_rate: DecimalInput | None = None,
        estimated_cost_rate: DecimalInput = Decimal("0"),
    ) -> StrategyDecision:
        """Choose directional alignment or fall back to funding-arbitrage review.

        A funding-arbitrage decision is eligible only when a positive funding
        rate exceeds the caller-provided estimated cost rate. This is a gate,
        not a claim that the strategy is profitable or an execution instruction.
        """
        if set(signals) != set(self.REQUIRED_TIMEFRAMES):
            raise ValueError("Signals must contain exactly 1m, 15m, and 1h.")
        if any(not isinstance(signal, Direction) for signal in signals.values()):
            raise ValueError("Every timeframe signal must be a Direction value.")
        aligned = signals["1m"]
        if aligned is not Direction.NEUTRAL and all(
            signals[timeframe] is aligned for timeframe in self.REQUIRED_TIMEFRAMES
        ):
            return StrategyDecision(
                StrategyMode.DIRECTIONAL,
                aligned,
                True,
                "All required timeframes agree.",
            )

        if funding_rate is None:
            return StrategyDecision(
                StrategyMode.FUNDING_ARBITRAGE,
                None,
                False,
                "Timeframes are not aligned and no funding rate was supplied.",
            )
        rate = _as_decimal(funding_rate, "funding_rate")
        cost = _as_decimal(estimated_cost_rate, "estimated_cost_rate")
        if cost < 0:
            raise ValueError("estimated_cost_rate must be non-negative.")
        eligible = rate > cost
        return StrategyDecision(
            StrategyMode.FUNDING_ARBITRAGE,
            None,
            eligible,
            (
                "Funding rate exceeds estimated costs."
                if eligible
                else "Funding rate does not exceed estimated costs."
            ),
        )


class MicroLatencyRouter:
    """Fail-closed placeholder for a future authenticated WebSocket executor."""

    async def route(self, order_intent: object) -> None:
        """Reject routing until a verified WebSocket execution adapter exists."""
        del order_intent
        raise NotImplementedError(
            "WebSocket order routing is not implemented; no order was submitted."
        )


def _format_sek(value: Decimal) -> str:
    """Format a SEK amount with two decimals and a currency suffix."""
    return f"{value:,.2f} SEK"


def run_monthly_simulation() -> AccountLedger:
    """Print the 12-month deposit-only example and return its final ledger."""
    ledger = AccountLedger()
    policy = RiskPolicy()
    print(
        "ILLUSTRATIVE SIMULATION ONLY — no live market data or orders; "
        "market PnL is assumed to be 0 SEK each month."
    )
    print(
        "Month | Deposits | Active Exchange | Cold Storage | "
        "Portfolio Value | Market PnL | Risk Phase"
    )
    print("-" * 112)

    for month in range(1, 13):
        if month in SCHEDULED_DEPOSITS_SEK:
            ledger.record_external_deposit(
                SCHEDULED_DEPOSITS_SEK[month],
                reference=f"scheduled-month-{month}",
            )
        ledger.sweep_eligible_milestones()
        snapshot = ledger.snapshot()
        phase = policy.profile_for(
            snapshot.total_portfolio_value_sek,
            "BTCUSDT",
        ).phase
        print(
            f"M{month:02d}   | "
            f"{_format_sek(snapshot.external_deposits_sek):>12} | "
            f"{_format_sek(snapshot.active_exchange_sek):>16} | "
            f"{_format_sek(snapshot.cold_storage_sek):>12} | "
            f"{_format_sek(snapshot.total_portfolio_value_sek):>16} | "
            f"{_format_sek(snapshot.market_generated_profit_sek):>12} | "
            f"{phase.value}"
        )
    return ledger


def verify_milestone_example() -> None:
    """Exercise all fixed sweeps and the exact 75,000 SEK milestone total."""
    ledger = AccountLedger()
    ledger.apply_market_pnl(Decimal("99000"))
    sweeps = ledger.sweep_eligible_milestones()
    snapshot = ledger.snapshot()
    expected_sweep_total = sum(
        (milestone.amount_sek for milestone in SWEEP_MILESTONES),
        Decimal("0"),
    )
    assert len(sweeps) == len(SWEEP_MILESTONES)
    assert snapshot.cold_storage_sek == Decimal("75000")
    assert snapshot.cold_storage_sek == expected_sweep_total
    assert ledger.sweep_eligible_milestones() == ()


def main() -> None:
    """Run the offline 12-month ledger example and verify sweep invariants."""
    if TOTAL_SCHEDULED_DEPOSITS_SEK != Decimal("20750"):
        raise RuntimeError("Scheduled deposits must total exactly 20,750 SEK.")
    verify_milestone_example()
    run_monthly_simulation()


if __name__ == "__main__":
    main()
