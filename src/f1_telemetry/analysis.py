"""Core telemetry loading, processing and visualisation functions."""

from __future__ import annotations

from pathlib import Path
import hashlib
import json

import numpy as np
import pandas as pd
import requests


REQUIRED_COLUMNS = {"Distance", "Time", "Speed", "Throttle", "Brake"}


def prepare_telemetry(telemetry: pd.DataFrame) -> pd.DataFrame:
    """Clean a telemetry frame and derive elapsed time and acceleration.

    Acceleration is estimated from the numerical derivative of speed. Speed is
    converted from km/h to m/s before differentiation.
    """
    missing = REQUIRED_COLUMNS.difference(telemetry.columns)
    if missing:
        raise ValueError(f"Missing telemetry columns: {sorted(missing)}")

    frame = telemetry.copy()
    if pd.api.types.is_timedelta64_dtype(frame["Time"]):
        frame["ElapsedSeconds"] = frame["Time"].dt.total_seconds()
    else:
        frame["ElapsedSeconds"] = pd.to_numeric(frame["Time"], errors="coerce")

    frame = (
        frame.dropna(subset=["Distance", "ElapsedSeconds", "Speed"])
        .sort_values("Distance")
        .drop_duplicates("Distance")
        .reset_index(drop=True)
    )
    if len(frame) < 3:
        raise ValueError("At least three valid telemetry samples are required")

    # Telemetry speed is quantised to integer km/h. A centred rolling mean
    # prevents numerical differentiation from turning those one-unit steps
    # into physically unrealistic acceleration spikes.
    smoothed_speed = frame["Speed"].rolling(window=9, center=True, min_periods=3).mean()
    speed_ms = smoothed_speed.to_numpy(dtype=float) / 3.6
    time_s = frame["ElapsedSeconds"].to_numpy(dtype=float)
    if np.any(np.diff(time_s) <= 0):
        raise ValueError("Telemetry time must increase strictly")
    frame["Acceleration_mps2"] = np.gradient(speed_ms, time_s)
    frame["Brake"] = frame["Brake"].astype(float)
    if frame["Brake"].max() <= 1:
        frame["Brake"] *= 100
    return frame


def align_telemetry(
    reference: pd.DataFrame,
    comparison: pd.DataFrame,
    points: int = 1500,
) -> pd.DataFrame:
    """Interpolate two prepared laps onto a shared distance grid."""
    if points < 10:
        raise ValueError("points must be at least 10")

    max_distance = min(reference["Distance"].max(), comparison["Distance"].max())
    min_distance = max(reference["Distance"].min(), comparison["Distance"].min())
    if max_distance <= min_distance:
        raise ValueError("The telemetry laps do not overlap in distance")

    distance = np.linspace(min_distance, max_distance, points)
    aligned = pd.DataFrame({"Distance": distance})
    channels = ["ElapsedSeconds", "Speed", "Throttle", "Brake", "Acceleration_mps2"]

    for prefix, frame in (("Reference", reference), ("Comparison", comparison)):
        source_distance = frame["Distance"].to_numpy(dtype=float)
        for channel in channels:
            aligned[f"{prefix}_{channel}"] = np.interp(
                distance, source_distance, frame[channel].to_numpy(dtype=float)
            )

    aligned["DeltaSeconds"] = (
        aligned["Comparison_ElapsedSeconds"] - aligned["Reference_ElapsedSeconds"]
    )
    return aligned


def calculate_metrics(aligned: pd.DataFrame, reference: str, comparison: str) -> pd.DataFrame:
    """Return concise engineering metrics for both aligned laps."""
    rows = []
    for prefix, driver in (("Reference", reference), ("Comparison", comparison)):
        rows.append(
            {
                "Driver": driver,
                "Lap time (s)": aligned[f"{prefix}_ElapsedSeconds"].iloc[-1],
                "Maximum speed (km/h)": aligned[f"{prefix}_Speed"].max(),
                "Mean speed (km/h)": aligned[f"{prefix}_Speed"].mean(),
                "Full throttle distance (%)": 100
                * (aligned[f"{prefix}_Throttle"] >= 99).mean(),
                "Braking distance (%)": 100 * (aligned[f"{prefix}_Brake"] >= 50).mean(),
                "Peak acceleration (m/s^2)": aligned[f"{prefix}_Acceleration_mps2"].max(),
                "Peak deceleration (m/s^2)": aligned[f"{prefix}_Acceleration_mps2"].min(),
            }
        )
    return pd.DataFrame(rows).set_index("Driver")


def _fetch_openf1(endpoint: str, params: dict, cache_dir: Path) -> list[dict]:
    """Fetch and cache one OpenF1 API response."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    identity = json.dumps([endpoint, sorted(params.items())], separators=(",", ":"))
    cache_file = cache_dir / f"{endpoint}_{hashlib.sha256(identity.encode()).hexdigest()[:16]}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))

    response = requests.get(
        f"https://api.openf1.org/v1/{endpoint}", params=params, timeout=90
    )
    response.raise_for_status()
    data = response.json()
    cache_file.write_text(json.dumps(data), encoding="utf-8")
    return data


def _find_session(year: int, event: str, session_code: str, cache_dir: Path) -> dict:
    session_names = {"R": "Race", "Q": "Qualifying", "FP1": "Practice 1",
                     "FP2": "Practice 2", "FP3": "Practice 3"}
    session_name = session_names.get(session_code.upper(), session_code)
    sessions = _fetch_openf1("sessions", {"year": year, "session_name": session_name}, cache_dir)
    target = event.casefold()
    for session in sessions:
        searchable = " ".join(str(session.get(key, "")) for key in (
            "country_name", "location", "circuit_short_name", "session_name"
        )).casefold()
        if target in searchable:
            return session
    choices = sorted({s.get("country_name", "Unknown") for s in sessions})
    raise ValueError(f"Event '{event}' not found. Available countries: {', '.join(choices)}")


def _driver_record(session_key: int, acronym: str, cache_dir: Path) -> dict:
    records = _fetch_openf1("drivers", {"session_key": session_key}, cache_dir)
    for record in records:
        if str(record.get("name_acronym", "")).upper() == acronym.upper():
            return record
    raise ValueError(f"Driver acronym '{acronym}' was not found in this session")


def _car_data_to_telemetry(records: list[dict], lap_start: str) -> pd.DataFrame:
    frame = pd.DataFrame(records)
    if frame.empty:
        raise ValueError("OpenF1 returned no car telemetry for the selected lap")
    dates = pd.to_datetime(frame["date"], utc=True, format="mixed")
    start = pd.to_datetime(lap_start, utc=True)
    elapsed = (dates - start).dt.total_seconds().to_numpy(dtype=float)
    speed_ms = frame["speed"].to_numpy(dtype=float) / 3.6
    dt = np.diff(elapsed)
    distance = np.zeros(len(frame), dtype=float)
    distance[1:] = np.cumsum((speed_ms[:-1] + speed_ms[1:]) * 0.5 * dt)
    telemetry = pd.DataFrame({
        "Distance": distance,
        "Time": elapsed,
        "Speed": frame["speed"].astype(float),
        "Throttle": frame["throttle"].astype(float),
        "Brake": frame["brake"].astype(float),
    })
    return prepare_telemetry(telemetry)


def load_fastest_laps(
    year: int,
    event: str,
    session_code: str,
    drivers: tuple[str, str],
    cache_dir: Path,
):
    """Load a session and return prepared fastest-lap telemetry for two drivers."""
    session = _find_session(year, event, session_code, cache_dir)
    session_key = int(session["session_key"])

    result = {}
    lap_info = {}
    for driver in drivers:
        driver_data = _driver_record(session_key, driver, cache_dir)
        driver_number = int(driver_data["driver_number"])
        laps = _fetch_openf1(
            "laps", {"session_key": session_key, "driver_number": driver_number}, cache_dir
        )
        valid_laps = [lap for lap in laps if lap.get("lap_duration") and not lap.get("is_pit_out_lap")]
        if not valid_laps:
            raise ValueError(f"No valid fastest lap found for {driver}")
        lap = min(valid_laps, key=lambda item: item["lap_duration"])
        later_laps = [item for item in laps if item["lap_number"] == lap["lap_number"] + 1]
        if later_laps:
            lap_end = later_laps[0]["date_start"]
        else:
            start = pd.to_datetime(lap["date_start"], utc=True)
            lap_end = (start + pd.to_timedelta(lap["lap_duration"], unit="s")).isoformat()
        records = _fetch_openf1(
            "car_data",
            {"session_key": session_key, "driver_number": driver_number,
             "date>": lap["date_start"], "date<": lap_end},
            cache_dir,
        )
        result[driver] = _car_data_to_telemetry(records, lap["date_start"])
        stints = _fetch_openf1(
            "stints", {"session_key": session_key, "driver_number": driver_number}, cache_dir
        )
        compound = next((s.get("compound", "Unknown") for s in stints
                         if s.get("lap_start", 10**9) <= lap["lap_number"] <= s.get("lap_end", -1)),
                        "Unknown")
        lap_info[driver] = {
            "lap_number": int(lap["lap_number"]),
            "official_lap_time": float(lap["lap_duration"]),
            "compound": str(compound),
        }
    return session, result, lap_info


def plot_comparison(aligned: pd.DataFrame, drivers: tuple[str, str], output: Path) -> None:
    """Create a four-panel telemetry comparison figure."""
    import matplotlib.pyplot as plt

    reference, comparison = drivers
    colors = {"Reference": "#2563eb", "Comparison": "#dc2626"}
    fig, axes = plt.subplots(4, 1, figsize=(13, 11), sharex=True,
                             gridspec_kw={"height_ratios": [2.2, 1, 1, 1.2]})
    channels = [("Speed", "Speed (km/h)"), ("Throttle", "Throttle (%)"), ("Brake", "Brake (%)")]
    for ax, (channel, ylabel) in zip(axes[:3], channels):
        ax.plot(aligned["Distance"], aligned[f"Reference_{channel}"],
                color=colors["Reference"], label=reference, linewidth=1.5)
        ax.plot(aligned["Distance"], aligned[f"Comparison_{channel}"],
                color=colors["Comparison"], label=comparison, linewidth=1.3, alpha=0.9)
        ax.set_ylabel(ylabel)
        ax.grid(alpha=0.2)

    axes[0].legend(ncol=2, frameon=False)
    axes[0].set_title(f"Fastest-lap telemetry: {reference} vs {comparison}", loc="left", weight="bold")
    axes[3].plot(aligned["Distance"], aligned["DeltaSeconds"], color="#111827", linewidth=1.5)
    axes[3].axhline(0, color="#6b7280", linewidth=0.8)
    axes[3].fill_between(aligned["Distance"], 0, aligned["DeltaSeconds"],
                         where=aligned["DeltaSeconds"] >= 0, color="#fecaca", alpha=0.7)
    axes[3].fill_between(aligned["Distance"], 0, aligned["DeltaSeconds"],
                         where=aligned["DeltaSeconds"] < 0, color="#bfdbfe", alpha=0.7)
    axes[3].set_ylabel(f"{comparison} delta (s)")
    axes[3].set_xlabel("Distance around lap (m)")
    axes[3].grid(alpha=0.2)
    fig.text(0.01, 0.01, "Positive delta: comparison driver is behind reference driver", fontsize=9)
    fig.tight_layout(rect=(0, 0.025, 1, 1))
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=180, bbox_inches="tight")
    plt.close(fig)

