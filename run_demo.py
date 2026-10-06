"""Create reproducible groundwater monitoring demos and run HydroGuard."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from hydroguard import HydroGuardConfig, HydroGuardPipeline


ROOT = Path(__file__).resolve().parent


def build_demo(structural_risk: bool = False) -> pd.DataFrame:
    rng = np.random.default_rng(202409)
    count = 720
    hour = np.arange(count)
    pumping = np.where(((hour // 72) % 2) == 1, 24.0, 0.0)
    recharge = np.where(((hour // 120) % 2) == 1, 12.0, 0.0)
    water = (
        12.40
        + 0.18 * np.sin(hour / 28.0)
        - 0.0065 * pumping
        + 0.0038 * recharge
        + rng.normal(0, 0.012, count)
    )
    frame = pd.DataFrame({
        "timestamp": pd.date_range("2024-09-01", periods=count, freq="h"),
        "water_level_m": water,
        "temperature_c": 19.5 + 2.2 * np.sin(hour / 80.0) + rng.normal(0, 0.08, count),
        "pumping_rate_m3h": pumping,
        "recharge_rate_m3h": recharge,
        "well_id": "CZ-03",
        "injected_anomaly": False,
    })

    # Known synthetic anomalies for a reproducible functional check.
    spike_positions = [96, 241, 516]
    frame.loc[spike_positions, "water_level_m"] += [1.9, -1.6, 2.2]
    frame.loc[310:311, "water_level_m"] = np.nan
    frame.loc[420:431, "water_level_m"] = frame.loc[419, "water_level_m"]
    frame.loc[spike_positions + list(range(310, 312)) + list(range(420, 432)), "injected_anomaly"] = True
    if structural_risk:
        frame.loc[600, "timestamp"] = frame.loc[599, "timestamp"]
        frame.loc[600, "injected_anomaly"] = True
    return frame


def main() -> None:
    examples = ROOT / "examples"
    outputs = ROOT / "outputs"
    examples.mkdir(exist_ok=True)
    summary = {}
    for name, risk in (("normal", False), ("structural_risk", True)):
        source = examples / f"groundwater_{name}.csv"
        build_demo(risk).to_csv(source, index=False)
        config = HydroGuardConfig(model_backend="auto")
        summary[name] = HydroGuardPipeline(config).run(source, outputs / name)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

