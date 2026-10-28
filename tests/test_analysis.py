import numpy as np
import pandas as pd
import pytest

from f1_telemetry.analysis import align_telemetry, calculate_metrics, prepare_telemetry


def sample_lap(offset=0.0, speed_shift=0.0):
    return pd.DataFrame(
        {
            "Distance": [0, 100, 200, 300, 400],
            "Time": pd.to_timedelta(np.array([0, 2, 4, 6, 8]) + offset, unit="s"),
            "Speed": np.array([100, 150, 200, 150, 100]) + speed_shift,
            "Throttle": [100, 100, 80, 20, 100],
            "Brake": [False, False, False, True, False],
        }
    )


def test_prepare_telemetry_derives_numeric_channels():
    result = prepare_telemetry(sample_lap())
    assert "ElapsedSeconds" in result
    assert "Acceleration_mps2" in result
    assert result["ElapsedSeconds"].iloc[-1] == pytest.approx(8.0)


def test_align_telemetry_delta_matches_known_offset():
    ref = prepare_telemetry(sample_lap())
    cmp = prepare_telemetry(sample_lap(offset=0.5))
    aligned = align_telemetry(ref, cmp, points=20)
    assert aligned["DeltaSeconds"].iloc[-1] == pytest.approx(0.5)


def test_calculate_metrics_has_both_drivers():
    ref = prepare_telemetry(sample_lap())
    cmp = prepare_telemetry(sample_lap(offset=0.2, speed_shift=-5))
    aligned = align_telemetry(ref, cmp, points=20)
    metrics = calculate_metrics(aligned, "AAA", "BBB")
    assert list(metrics.index) == ["AAA", "BBB"]
    assert metrics.loc["AAA", "Maximum speed (km/h)"] > metrics.loc["BBB", "Maximum speed (km/h)"]


def test_missing_channel_is_rejected():
    with pytest.raises(ValueError, match="Missing telemetry columns"):
        prepare_telemetry(sample_lap().drop(columns="Brake"))

