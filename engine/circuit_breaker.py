from __future__ import annotations

import math
from datetime import date


class CircuitBreaker:
    """Latch trade entry on daily drawdown or an elevated P14 safety score."""

    def __init__(
        self,
        *,
        max_drawdown_pct: float = -0.02,
        p14_threshold: float = 0.95,
    ) -> None:
        max_drawdown_pct = self._finite_number(
            max_drawdown_pct, "Maximum drawdown"
        )
        p14_threshold = self._finite_number(p14_threshold, "P14 threshold")
        if not -1 < max_drawdown_pct < 0:
            raise ValueError("Maximum drawdown must be a finite percentage in (-1, 0).")
        if not 0 <= p14_threshold <= 1:
            raise ValueError("P14 threshold must be a finite value in [0, 1].")
        self.max_drawdown_pct = max_drawdown_pct
        self.p14_threshold = p14_threshold
        self._drawdown_date: date | None = None
        self._drawdown_latched = False
        self._p14_latched = False
        self.drawdown_reason: str | None = None
        self.p14_reason: str | None = None
        self.reason: str | None = None

    @property
    def halted(self) -> bool:
        return self._drawdown_latched or self._p14_latched

    @property
    def drawdown_halted(self) -> bool:
        return self._drawdown_latched

    @property
    def p14_latched(self) -> bool:
        return self._p14_latched

    def check_daily_drawdown(
        self, equity: float, start_equity: float, trading_date: date
    ) -> bool:
        equity = self._finite_number(equity, "Current equity")
        start_equity = self._finite_number(start_equity, "Starting equity")
        if equity <= 0:
            raise ValueError("Current equity must be positive and finite.")
        if start_equity <= 0:
            raise ValueError("Starting equity must be positive and finite.")
        if not isinstance(trading_date, date):
            raise TypeError("Trading date must be a date.")
        if self._drawdown_date != trading_date:
            self._drawdown_date = trading_date
            self._drawdown_latched = False
            self.drawdown_reason = None
            self.reason = self.p14_reason
        drawdown = equity / start_equity - 1
        if drawdown <= self.max_drawdown_pct:
            self._drawdown_latched = True
            self.drawdown_reason = (
                f"Daily drawdown {drawdown:.2%} reached the "
                f"{self.max_drawdown_pct:.2%} limit."
            )
            self.reason = self.p14_reason or self.drawdown_reason
        return not self._drawdown_latched

    def check_p14(self, score: float) -> bool:
        score = self._finite_number(score, "P14 safety score")
        if not 0 <= score <= 1:
            raise ValueError("P14 safety score must be finite and normalized to [0, 1].")
        if score >= self.p14_threshold:
            self._p14_latched = True
            self.p14_reason = (
                f"P14 safety threshold reached ({score:.3f} >= "
                f"{self.p14_threshold:.3f}); manual reset required."
            )
            self.reason = self.p14_reason
        return not self._p14_latched

    def reset_p14(self) -> None:
        """Explicit operator reset; daily drawdown remains latched for the day."""
        self._p14_latched = False
        self.p14_reason = None
        self.reason = self.drawdown_reason

    @staticmethod
    def _finite_number(value: object, label: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{label} must be a finite number.")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"{label} must be a finite number.")
        return number
