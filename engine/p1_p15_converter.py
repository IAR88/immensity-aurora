from __future__ import annotations

import math
from collections.abc import Mapping

PREDICTIVE_PARAMETER_NAMES = {
    "P1": "github_commit_velocity",
    "P2": "arxiv_preprint_rate",
    "P3": "tech_media_mention_index",
    "P4": "eu_directive_impact_score",
    "P5": "subsidy_tuff_index",
    "P6": "regulatory_compliance_lag",
    "P7": "viral_velocity_score",
    "P8": "marketplace_shortage_index",
    "P9": "lifestyle_shift_rate",
    "P10": "supply_chain_bottleneck_index",
    "P11": "weather_anomaly_impact",
    "P12": "commodity_flow_velocity",
    "P13": "cross_domain_correlation_coefficient",
    "P14": "public_reaction_speed",
    "P15": "trend_saturation_level",
}


class P1P15Converter:
    """Validate and normalize externally provided P1-P15 values."""

    @classmethod
    def convert(cls, parameters: Mapping[str, object]) -> dict[str, float]:
        if not isinstance(parameters, Mapping):
            raise TypeError("P1-P15 parameters must be supplied as a mapping.")

        by_name = {name: code for code, name in PREDICTIVE_PARAMETER_NAMES.items()}
        converted: dict[str, float] = {}
        for key, raw_value in parameters.items():
            if not isinstance(key, str):
                raise ValueError("P1-P15 parameter keys must be strings.")
            code = key.upper()
            name = PREDICTIVE_PARAMETER_NAMES.get(code)
            if name is None:
                name = key
                code = by_name.get(name, "")
            if not code:
                raise ValueError(f"Unknown P1-P15 parameter: {key!r}.")
            if name in converted:
                raise ValueError(f"Duplicate value supplied for {name}.")
            if isinstance(raw_value, bool):
                raise ValueError(f"{name} must be a finite numeric value.")
            try:
                value = float(raw_value)
            except (TypeError, ValueError) as error:
                raise ValueError(f"{name} must be a finite numeric value.") from error
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite.")

            if name == "trend_saturation_level":
                if value not in (1, 2, 3):
                    raise ValueError("trend_saturation_level must be 1, 2, or 3.")
            elif not 0 <= value <= 1:
                raise ValueError(f"{name} must be normalized to [0, 1].")
            converted[name] = value

        missing = set(PREDICTIVE_PARAMETER_NAMES.values()) - converted.keys()
        if missing:
            raise ValueError(
                "Missing P1-P15 parameters: " + ", ".join(sorted(missing))
            )
        return converted
