from __future__ import annotations

import pandas as pd

from scripts.fetch_open_meteo import append_raw


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
