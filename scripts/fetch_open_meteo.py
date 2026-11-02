from __future__ import annotations

import argparse
import math
import json
import os
import sys
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


def request_json(url: str, params: dict[str, Any], *, timeout: int, retries: int) -> Any:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            # A batched call contains four locations. Pace recovery below the
            # public endpoint's per-minute request allowance.
            time.sleep(0.5)
            response = requests.get(url, params=params, timeout=timeout)
            if response.status_code == 429:
                time.sleep(min(float(response.headers.get("Retry-After", 60)), 60))
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
    params = {
        "latitude": ",".join(str(float(x)) for x in points.gfs_lat),
        "longitude": ",".join(str(float(x)) for x in points.gfs_lon),
        "hourly": ",".join(config.hourly),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "timezone": config.timezone,
        "wind_speed_unit": config.wind_speed_unit,
    }
    try:
        payloads = request_json(url, params, timeout=min(config.timeout, 15), retries=1)
    except RuntimeError:
        if start_date < end_date:
            # A small daily request can succeed when a multi-day query stalls.
            # Keep the same endpoint, points, variables and model policy.
            print("Multi-day request stalled: retrying individual UTC days", flush=True)
            days = []
            cursor = start_date
            while cursor <= end_date:
                days.append(fetch_endpoint(url=url, points=points, start_date=cursor,
                                           end_date=cursor, config=config))
                cursor += timedelta(days=1)
            return pd.concat(days, ignore_index=True)
        # Some multi-location requests stall even when the individual locations
        # work. Do not repeat the same stalled batch for several minutes.
        print(f"Batch timeout/error: retrying {len(points)} locations individually", flush=True)
        payloads = [request_json(url, {**params, "latitude": float(p.gfs_lat),
                                      "longitude": float(p.gfs_lon)},
                                 timeout=min(config.timeout, 30), retries=min(config.retries, 2))
                    for p in points.itertuples(index=False)]
    if isinstance(payloads, dict):
        payloads = [payloads]
    if len(payloads) != len(points):
        raise ValueError("Open-Meteo returned an unexpected number of locations")
    for point, payload in zip(points.itertuples(index=False), payloads, strict=True):
        point_no = int(point.point)
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
    weather = weather.sort_values("timestamp_utc").reset_index(drop=True)
    expected = pd.date_range(start_date, end_date + timedelta(days=1), freq="h", tz="UTC", inclusive="left")
    if (weather.timestamp_utc.duplicated().any()
            or not pd.DatetimeIndex(weather.timestamp_utc).equals(expected)
            or not np.isfinite(weather.drop(columns="timestamp_utc").to_numpy(dtype=float)).all()):
        raise ValueError("Incomplete/non-finite Open-Meteo hourly grid")
    return weather


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
    atomic_csv(frame, path)
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
        part = part.drop_duplicates("timestamp_utc", keep="last").sort_values("timestamp_utc")
        atomic_csv(part, path)


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
    atomic_csv(manifest, MANIFEST_PATH)


def atomic_csv(frame: pd.DataFrame, path: Path) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temp, index=False)
    temp.replace(path)


def configure_data_dir(path: Path) -> None:
    global DATA_DIR, RAW_DIR, UPDATES_DIR, MANIFEST_PATH
    DATA_DIR = path.resolve()
    RAW_DIR = DATA_DIR / "raw"
    UPDATES_DIR = DATA_DIR / "updates"
    MANIFEST_PATH = DATA_DIR / "update_manifest.csv"


def valid_existing(country: str, target: str) -> pd.DatetimeIndex:
    frames = []
    for path in (RAW_DIR / country / target).glob("*.csv"):
        frame = pd.read_csv(path)
        values = frame.filter(regex=r"_p[0-9]+$")
        if len(values.columns) < 4 * 6:
            continue
        valid = np.isfinite(values.to_numpy(dtype=float)).all(axis=1)
        frames.append(pd.to_datetime(frame.loc[valid, "timestamp_utc"], utc=True))
    return pd.DatetimeIndex(pd.concat(frames).drop_duplicates()) if frames else pd.DatetimeIndex([], tz="UTC")


def history_windows(country: str, target: str, start: date, end: date, chunk_days: int):
    existing = valid_existing(country, target)
    cursor = start
    pending = []
    while cursor <= end:
        grid = pd.date_range(cursor, cursor + timedelta(days=1), freq="h", tz="UTC", inclusive="left")
        if not grid.isin(existing).all():
            pending.append(cursor)
        cursor += timedelta(days=1)
    first = last = None
    for day in pending:
        if last is None or day != last + timedelta(days=1) or (day - first).days >= chunk_days:
            if last is not None:
                yield first, last
            first = day
        last = day
    if last is not None:
        yield first, last


def expected_scopes(app_config, countries=None, targets=None):
    excluded = {(x["country"], x["target"]) for x in app_config.get("excluded_series", [])}
    return [(c, t) for c in (countries or app_config["countries"])
            for t in (targets or app_config["targets"]) if (c, t) not in excluded]


def collect(app_config, fetch_config, args) -> dict:
    start, end = date_window(args, app_config["defaults"])
    if end < start:
        raise ValueError("end date must be after start date")
    today = datetime.now(UTC).date()
    scopes = expected_scopes(app_config, args.countries.split(",") if args.countries else None,
                             args.targets.split(",") if args.targets else None)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    failures, completed = [], []
    # Refresh every current forecast first. Historical failures cannot block it.
    phases = [p for p in ["forecast", "history"] if args.phase in {"all", p}]
    for phase in phases:
        for country, target in scopes:
            windows = ([(max(start, today), end)] if end >= today else []) if phase == "forecast" else list(
                history_windows(country, target, start, min(end, today - timedelta(days=1)), args.chunk_days))
            for begin, stop in windows:
                try:
                    frame = fetch_weather(country, target, begin, stop, fetch_config)
                    if frame.empty:
                        raise ValueError("Empty response")
                    if phase == "history":
                        # Recovered history is not an issue-time vintage. Preserve existing good rows.
                        frame = frame[~pd.to_datetime(frame.timestamp_utc, utc=True).isin(valid_existing(country, target))]
                    if frame.empty:
                        continue
                    update_path = write_update(frame, country, target, run_id + "_" + begin.isoformat())
                    append_raw(frame, country, target)
                    append_manifest([dict(
                        run_id=run_id, collection_time_utc=datetime.now(UTC).isoformat(timespec="seconds"),
                        country=country, target=target, rows=len(frame),
                        window_start_utc=pd.to_datetime(frame.timestamp_utc, utc=True).min().isoformat(),
                        window_end_utc=pd.to_datetime(frame.timestamp_utc, utc=True).max().isoformat(),
                        path="data/" + str(update_path.relative_to(DATA_DIR)),
                        endpoint=fetch_config.forecast_url if phase == "forecast" else fetch_config.historical_forecast_url,
                        model_selection="automatic_best_match", phase=phase,
                        vintage="collected_forecast" if phase == "forecast" else "retrospective_historical_forecast",
                    )])
                    completed.append(dict(country=country, target=target, phase=phase, rows=len(frame)))
                    print(f"{country} {target} {phase}: saved {len(frame)} hours ({begin} to {stop})", flush=True)
                except Exception as exc:
                    failures.append(dict(country=country, target=target, phase=phase,
                                         start=str(begin), end=str(stop), error=str(exc)))
                    print(f"FAILED {country} {target} {phase}: {exc}", flush=True)
    # Verify saved coverage, not merely whether requests returned HTTP 200.
    coverage = []
    for country, target in scopes:
        existing = valid_existing(country, target)
        for phase in phases:
            begin, stop = (max(start, today), end) if phase == "forecast" else (start, min(end, today - timedelta(days=1)))
            grid = pd.date_range(begin, stop + timedelta(days=1), freq="h", tz="UTC", inclusive="left") if begin <= stop else []
            missing = len(pd.DatetimeIndex(grid).difference(existing))
            coverage.append(dict(country=country, target=target, phase=phase, missing_hours=missing))
    status = dict(run_id=run_id, generated_at_utc=datetime.now(UTC).isoformat(timespec="seconds"),
                  window_start=str(start), window_end=str(end), completed=completed,
                  failures=failures, coverage=coverage,
                  complete=not failures and all(x["missing_hours"] == 0 for x in coverage))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp = DATA_DIR / "status.json.tmp"
    temp.write_text(json.dumps(status, indent=2) + "\n")
    temp.replace(DATA_DIR / "status.json")
    return status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch 4-point Open-Meteo weather data.")
    parser.add_argument("--countries", default=None, help="Comma-separated countries. Defaults to config countries.")
    parser.add_argument("--targets", default=None, help="Comma-separated targets. Defaults to config targets.")
    parser.add_argument("--start-date", type=lambda value: datetime.strptime(value, "%Y-%m-%d").date(), default=None)
    parser.add_argument("--end-date", type=lambda value: datetime.strptime(value, "%Y-%m-%d").date(), default=None)
    parser.add_argument("--rolling-days", type=int, default=None)
    parser.add_argument("--forecast-days", type=int, default=None)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--phase", choices=["all", "forecast", "history"], default="all")
    parser.add_argument("--chunk-days", type=int, default=3)
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
    if args.chunk_days < 1:
        raise ValueError("chunk-days must be positive")
    configure_data_dir(args.data_dir)
    status = collect(app_config, fetch_config, args)
    if not status["complete"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
