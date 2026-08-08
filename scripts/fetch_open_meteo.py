from __future__ import annotations

import argparse
import math
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests
import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"
DATA_DIR = REPO_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
UPDATES_DIR = DATA_DIR / "updates"
MANIFEST_PATH = DATA_DIR / "update_manifest.csv"


@dataclass(frozen=True)
class FetchConfig:
    forecast_url: str
    historical_forecast_url: str
    hourly: list[str]
    timezone: str
    wind_speed_unit: str
    timeout: int
    retries: int


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_config() -> tuple[dict[str, Any], FetchConfig]:
    config = load_yaml(CONFIG_DIR / "targets.yml")
    open_meteo = config["open_meteo"]
    defaults = config["defaults"]
    fetch_config = FetchConfig(
        forecast_url=open_meteo["forecast_url"],
        historical_forecast_url=open_meteo["historical_forecast_url"],
        hourly=open_meteo["hourly"],
        timezone=open_meteo["timezone"],
        wind_speed_unit=open_meteo["wind_speed_unit"],
        timeout=int(defaults["request_timeout_seconds"]),
        retries=int(defaults["retries"]),
    )
    return config, fetch_config


def selected_points(country: str, target: str) -> pd.DataFrame:
    if target == "load":
        path = CONFIG_DIR / "selected_load_weather_points_for_tp_v01_4p.csv"
    else:
        path = CONFIG_DIR / "selected_weather_points_for_tp_v01_4p.csv"
    points = pd.read_csv(path)
    subset = points[(points["country"].eq(country)) & (points["type"].eq(target))].copy()
    subset = subset.sort_values("point")
    if subset.empty:
        raise ValueError(f"No selected weather points for {country} {target}")
    return subset


def request_json(url: str, params: dict[str, Any], *, timeout: int, retries: int) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = requests.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last_error = exc
            time.sleep(min(2**attempt, 8))
    raise RuntimeError(f"Open-Meteo request failed after {retries} attempts: {last_error}")


def fetch_endpoint(
    *,
    url: str,
    points: pd.DataFrame,
    start_date: date,
    end_date: date,
    config: FetchConfig,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for point in points.itertuples(index=False):
        point_no = int(point.point)
        payload = request_json(
            url,
            {
                "latitude": float(point.gfs_lat),
                "longitude": float(point.gfs_lon),
                "hourly": ",".join(config.hourly),
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "timezone": config.timezone,
                "wind_speed_unit": config.wind_speed_unit,
            },
            timeout=config.timeout,
            retries=config.retries,
        )
        hourly = payload.get("hourly", {})
        if "time" not in hourly:
            raise RuntimeError(f"Open-Meteo response has no hourly time field for point {point_no}")
        frame = pd.DataFrame({"timestamp_utc": pd.to_datetime(hourly["time"], utc=True)})
        for variable in config.hourly:
            values = pd.to_numeric(pd.Series(hourly.get(variable, [])), errors="coerce")
            if variable == "wind_speed_100m":
                frame[f"wind_speed_100m_ms_p{point_no}"] = values
            elif variable == "wind_direction_100m":
                radians = np.deg2rad(values.astype(float))
                frame[f"wind_dir_sin_p{point_no}"] = np.sin(radians)
                frame[f"wind_dir_cos_p{point_no}"] = np.cos(radians)
            else:
                frame[f"{variable}_p{point_no}"] = values
        frames.append(frame)

    weather = frames[0]
    for frame in frames[1:]:
        weather = weather.merge(frame, on="timestamp_utc", how="outer")
    return weather.sort_values("timestamp_utc").reset_index(drop=True)


def fetch_weather(country: str, target: str, start: date, end: date, config: FetchConfig) -> pd.DataFrame:
    points = selected_points(country, target)
    today = datetime.now(UTC).date()
    parts: list[pd.DataFrame] = []
    if start < today:
        historical_end = min(end, today - timedelta(days=1))
        if historical_end >= start:
            parts.append(
                fetch_endpoint(
                    url=config.historical_forecast_url,
                    points=points,
                    start_date=start,
                    end_date=historical_end,
                    config=config,
                )
            )
    if end >= today:
        forecast_start = max(start, today)
        parts.append(
            fetch_endpoint(
                url=config.forecast_url,
                points=points,
                start_date=forecast_start,
                end_date=end,
                config=config,
            )
        )
    if not parts:
        return pd.DataFrame()

    frame = pd.concat(parts, ignore_index=True)
    frame = frame.drop_duplicates("timestamp_utc", keep="last").sort_values("timestamp_utc")
    temp_cols = [column for column in frame.columns if column.startswith("temperature_2m_p")]
    if temp_cols:
        frame["deg_proxy"] = (frame[temp_cols].mean(axis=1) - 18.0).abs()
    frame.insert(1, "country", country)
    frame.insert(2, "target", target)
    frame["source"] = "open-meteo"
    frame["updated_at_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
    return frame.reset_index(drop=True)


def write_update(frame: pd.DataFrame, country: str, target: str, run_id: str) -> Path:
    if frame.empty:
        raise ValueError("Cannot write empty update frame")
    start = pd.to_datetime(frame["timestamp_utc"], utc=True).min()
    path = UPDATES_DIR / country / target / f"{start.year:04d}" / f"{start.month:02d}" / f"{run_id}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)
    return path


def append_raw(frame: pd.DataFrame, country: str, target: str) -> None:
    if frame.empty:
        return
    frame = frame.copy()
    frame["timestamp_utc"] = pd.to_datetime(frame["timestamp_utc"], utc=True)
    for year, part in frame.groupby(frame["timestamp_utc"].dt.year):
        path = RAW_DIR / country / target / f"{int(year):04d}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = pd.read_csv(path)
            existing["timestamp_utc"] = pd.to_datetime(existing["timestamp_utc"], utc=True)
            part = pd.concat([existing, part], ignore_index=True)
        part = part.sort_values("timestamp_utc").drop_duplicates("timestamp_utc", keep="last")
        part.to_csv(path, index=False)


def append_manifest(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    new = pd.DataFrame(rows)
    if MANIFEST_PATH.exists():
        manifest = pd.concat([pd.read_csv(MANIFEST_PATH), new], ignore_index=True)
    else:
        manifest = new
    manifest = manifest.drop_duplicates(["run_id", "country", "target", "path"], keep="last")
    manifest.to_csv(MANIFEST_PATH, index=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch 4-point Open-Meteo weather data.")
    parser.add_argument("--countries", default=None, help="Comma-separated countries. Defaults to config countries.")
    parser.add_argument("--targets", default=None, help="Comma-separated targets. Defaults to config targets.")
    parser.add_argument("--start-date", type=lambda value: datetime.strptime(value, "%Y-%m-%d").date(), default=None)
    parser.add_argument("--end-date", type=lambda value: datetime.strptime(value, "%Y-%m-%d").date(), default=None)
    parser.add_argument("--rolling-days", type=int, default=None)
    parser.add_argument("--forecast-days", type=int, default=None)
    return parser.parse_args()


def date_window(args: argparse.Namespace, defaults: dict[str, Any]) -> tuple[date, date]:
    if args.start_date and args.end_date:
        return args.start_date, args.end_date
    today = datetime.now(UTC).date()
    rolling_days = int(args.rolling_days or defaults["rolling_days"])
    forecast_days = int(args.forecast_days or defaults["forecast_days"])
    return today - timedelta(days=rolling_days), today + timedelta(days=forecast_days)


def main() -> None:
    app_config, fetch_config = load_config()
    args = parse_args()
    countries = args.countries.split(",") if args.countries else app_config["countries"]
    targets = args.targets.split(",") if args.targets else app_config["targets"]
    start, end = date_window(args, app_config["defaults"])
    if end < start:
        raise ValueError("end date must be after start date")

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    manifest_rows: list[dict[str, Any]] = []
    for country in countries:
        for target in targets:
            frame = fetch_weather(country, target, start, end, fetch_config)
            if frame.empty:
                continue
            update_path = write_update(frame, country, target, run_id)
            append_raw(frame, country, target)
            manifest_rows.append(
                {
                    "run_id": run_id,
                    "collection_time_utc": datetime.now(UTC).isoformat(timespec="seconds"),
                    "country": country,
                    "target": target,
                    "rows": len(frame),
                    "window_start_utc": pd.to_datetime(frame["timestamp_utc"], utc=True).min().isoformat(),
                    "window_end_utc": pd.to_datetime(frame["timestamp_utc"], utc=True).max().isoformat(),
                    "path": str(update_path.relative_to(REPO_ROOT)),
                }
            )
            print(f"{country} {target}: wrote {len(frame)} rows")
    append_manifest(manifest_rows)


if __name__ == "__main__":
    main()
