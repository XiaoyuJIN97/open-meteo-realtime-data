from __future__ import annotations

from datetime import date

import pandas as pd

from scripts.fetch_open_meteo import FetchConfig, append_raw, fetch_endpoint_with_fallback


def test_append_raw_deduplicates_year(tmp_path, monkeypatch):
    import scripts.fetch_open_meteo as fetcher

    monkeypatch.setattr(fetcher, "RAW_DIR", tmp_path / "raw")
    frame = pd.DataFrame(
        {
            "timestamp_utc": ["2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"],
            "country": ["BE", "BE"],
            "target": ["load", "load"],
            "temperature_2m_p1": [1.0, 2.0],
        }
    )
    append_raw(frame, "BE", "load")
    out = pd.read_csv(tmp_path / "raw" / "BE" / "load" / "2026.csv")
    assert len(out) == 1
    assert out["temperature_2m_p1"].iloc[0] == 2.0


def test_fetch_endpoint_falls_back_to_daily_chunks(monkeypatch):
    import scripts.fetch_open_meteo as fetcher

    calls = []

    def fake_fetch_endpoint(*, url, points, start_date, end_date, config):
        calls.append((start_date, end_date))
        if start_date != end_date:
            raise RuntimeError("chunk failed")
        return pd.DataFrame({"timestamp_utc": [pd.Timestamp(start_date)]})

    monkeypatch.setattr(fetcher, "fetch_endpoint", fake_fetch_endpoint)
    points = pd.DataFrame({"point": [1], "gfs_lat": [50.0], "gfs_lon": [4.0]})
    config = FetchConfig(
        forecast_url="forecast",
        historical_forecast_url="historical",
        hourly=["temperature_2m"],
        timezone="UTC",
        wind_speed_unit="ms",
        timeout=90,
        retries=5,
        chunk_days=7,
    )

    frames = fetch_endpoint_with_fallback(
        url="historical",
        points=points,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 3),
        config=config,
    )

    assert calls == [
        (date(2026, 8, 1), date(2026, 8, 3)),
        (date(2026, 8, 1), date(2026, 8, 1)),
        (date(2026, 8, 2), date(2026, 8, 2)),
        (date(2026, 8, 3), date(2026, 8, 3)),
    ]
    assert len(frames) == 3
