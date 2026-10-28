#!/usr/bin/env python3
"""Run the complete F1 telemetry comparison."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from f1_telemetry.analysis import (  # noqa: E402
    align_telemetry,
    calculate_metrics,
    load_fastest_laps,
    plot_comparison,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=2024)
    parser.add_argument("--event", default="Bahrain")
    parser.add_argument("--session", default="R", help="R, Q, FP1, FP2 or FP3")
    parser.add_argument("--drivers", nargs=2, default=["VER", "PER"], metavar=("REF", "CMP"))
    return parser.parse_args()


def write_summary(path: Path, title: str, drivers: tuple[str, str], metrics, lap_info, delta: float) -> None:
    ref, cmp = drivers
    faster = ref if delta >= 0 else cmp
    lines = [
        f"# {title}",
        "",
        "## Result",
        "",
        f"The distance-aligned telemetry gives a final delta of **{abs(delta):.3f} s** in favour of **{faster}**.",
        f"The selected laps were lap {lap_info[ref]['lap_number']} ({lap_info[ref]['compound']}) for {ref} "
        f"and lap {lap_info[cmp]['lap_number']} ({lap_info[cmp]['compound']}) for {cmp}.",
        "",
        "## Quantitative observations",
        "",
        f"- Maximum speeds: {ref} {metrics.loc[ref, 'Maximum speed (km/h)']:.1f} km/h; "
        f"{cmp} {metrics.loc[cmp, 'Maximum speed (km/h)']:.1f} km/h.",
        f"- Mean speeds: {ref} {metrics.loc[ref, 'Mean speed (km/h)']:.1f} km/h; "
        f"{cmp} {metrics.loc[cmp, 'Mean speed (km/h)']:.1f} km/h.",
        f"- Full-throttle distance: {ref} {metrics.loc[ref, 'Full throttle distance (%)']:.1f}%; "
        f"{cmp} {metrics.loc[cmp, 'Full throttle distance (%)']:.1f}%.",
        f"- Braking distance: {ref} {metrics.loc[ref, 'Braking distance (%)']:.1f}%; "
        f"{cmp} {metrics.loc[cmp, 'Braking distance (%)']:.1f}%.",
        "",
        "## Interpretation and limitations",
        "",
        "The plot shows where the lap-time gap grows, but it does not by itself prove why it grows. "
        "Fuel load, tyre condition, traffic, energy deployment and telemetry sampling can all affect the comparison. "
        "Using same-team drivers reduces car-performance differences, but it does not remove these limitations.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    drivers = tuple(driver.upper() for driver in args.drivers)
    session, telemetry, lap_info = load_fastest_laps(
        args.year, args.event, args.session.upper(), drivers, PROJECT_ROOT / "cache"
    )
    aligned = align_telemetry(telemetry[drivers[0]], telemetry[drivers[1]])
    metrics = calculate_metrics(aligned, *drivers)

    output_dir = PROJECT_ROOT / "outputs"
    output_dir.mkdir(exist_ok=True)
    plot_comparison(aligned, drivers, output_dir / "telemetry_comparison.png")
    metrics.to_csv(output_dir / "metrics.csv", float_format="%.3f")
    title = f"{args.year} {session['country_name']} Grand Prix — {drivers[0]} vs {drivers[1]}"
    write_summary(
        output_dir / "analysis_summary.md", title, drivers, metrics, lap_info,
        aligned["DeltaSeconds"].iloc[-1],
    )
    print(title)
    print(metrics.round(3).to_string())
    print(f"\nResults saved in: {output_dir}")


if __name__ == "__main__":
    main()

