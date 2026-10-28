from __future__ import annotations

import math
import time


class OpportunityCostOptimizer:
    """Gate position reallocation by holding cooldown and net ROI advantage."""

    def __init__(
        self,
        fee_hurdle: float = 0.0012,
        safety_margin: float = 0.005,
        cooldown_seconds: float = 60.0,
    ) -> None:
        self.fee_hurdle = self._finite_number(fee_hurdle, "fee_hurdle")
        self.safety_margin = self._finite_number(safety_margin, "safety_margin")
        self.cooldown_seconds = self._finite_number(
            cooldown_seconds, "cooldown_seconds"
        )
        if self.fee_hurdle < 0:
            raise ValueError("fee_hurdle must be non-negative.")
        if self.safety_margin < 0:
            raise ValueError("safety_margin must be non-negative.")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be non-negative.")

    def evaluate_reallocation(
        self, active_position: dict, new_signal: dict
    ) -> bool:
        if not isinstance(active_position, dict):
            raise TypeError("active_position must be a dict.")
        if not isinstance(new_signal, dict):
            raise TypeError("new_signal must be a dict.")

        entry_time = self._finite_number(
            active_position.get("entry_time"), "entry_time"
        )
        held_seconds = time.time() - entry_time
        if held_seconds < self.cooldown_seconds:
            return False

        current_roi = self._finite_number(
            active_position.get("current_roi"), "current_roi"
        )
        new_roi = self._finite_number(new_signal.get("new_roi"), "new_roi")
        net_advantage = new_roi - current_roi
        return net_advantage > self.fee_hurdle + self.safety_margin

    @staticmethod
    def _finite_number(value: object, field: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{field} must be a finite number.")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"{field} must be a finite number.")
        return number
