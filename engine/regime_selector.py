from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Regime:
    level: int
    name: str
    entry_allowed: bool


class RegimeSelector:
    """Select the configured P15 meta-regime and fail closed on bad config."""

    def __init__(
        self, regimes: dict[int, Regime], *, p14_threshold: float = 0.95
    ) -> None:
        if set(regimes) != {1, 2, 3}:
            raise ValueError("P15 configuration must define regimes 1, 2, and 3.")
        if not math.isfinite(p14_threshold) or not 0 <= p14_threshold <= 1:
            raise ValueError("P14 threshold must be finite and normalized to [0, 1].")
        self._regimes = regimes
        self.p14_threshold = p14_threshold

    @classmethod
    def from_file(cls, path: str | Path) -> RegimeSelector:
        try:
            with Path(path).open(encoding="utf-8") as config_file:
                data: Any = json.load(config_file)
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Could not load P15 regime configuration: {path}.") from error
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise ValueError("Unsupported P15 regime configuration schema.")
        settings = data.get("p15_regimes")
        if not isinstance(settings, dict):
            raise ValueError("P15 configuration must contain p15_regimes.")
        safety = data.get("p14_safety")
        if not isinstance(safety, dict):
            raise ValueError("Configuration must contain p14_safety.")
        threshold = safety.get("halt_at_or_above")
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not math.isfinite(threshold)
        ):
            raise ValueError("P14 halt_at_or_above must be a finite number.")
        regimes: dict[int, Regime] = {}
        for raw_level, config in settings.items():
            try:
                level = int(raw_level)
            except (TypeError, ValueError) as error:
                raise ValueError(f"Invalid P15 regime level: {raw_level!r}.") from error
            if not isinstance(config, dict):
                raise ValueError(f"P15 regime {level} must be an object.")
            name = config.get("name")
            allowed = config.get("entry_allowed")
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"P15 regime {level} needs a non-empty name.")
            if not isinstance(allowed, bool):
                raise ValueError(f"P15 regime {level} needs boolean entry_allowed.")
            regimes[level] = Regime(level, name.strip(), allowed)
        return cls(regimes, p14_threshold=float(threshold))

    def select(self, level: float) -> Regime:
        if isinstance(level, bool) or not isinstance(level, (int, float)):
            raise ValueError("P15 regime level must be numeric.")
        if not math.isfinite(level) or not float(level).is_integer():
            raise ValueError("P15 regime level must be a finite integer.")
        selected = self._regimes.get(int(level))
        if selected is None:
            raise ValueError("P15 regime level must be configured as 1, 2, or 3.")
        return selected
