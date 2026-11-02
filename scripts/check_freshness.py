"""Check both latest forecast retrieval time and complete saved weather coverage."""
from __future__ import annotations
import argparse
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd
from scripts import fetch_open_meteo as fetcher


def is_fresh(data_dir: Path, *, now=None, rolling_days=100, forecast_days=3) -> bool:
    now = now or datetime.now(UTC)
    local = now.astimezone(ZoneInfo('Europe/Brussels'))
    cutoff = datetime.combine(local.date(), time(16, 30), ZoneInfo('Europe/Brussels'))
    manifest_path = data_dir / 'update_manifest.csv'
    if not manifest_path.exists():
        return False
    manifest = pd.read_csv(manifest_path)
    if not {'phase', 'collection_time_utc'}.issubset(manifest.columns):
        return False
    recent = manifest[(manifest.phase == 'forecast') &
                      (pd.to_datetime(manifest.collection_time_utc, utc=True) >= cutoff)]
    seen = set(zip(recent.country, recent.target))
    config, _ = fetcher.load_config()
    fetcher.configure_data_dir(data_dir)
    today = now.astimezone(UTC).date()
    grid = pd.date_range(today - timedelta(days=rolling_days),
                         today + timedelta(days=forecast_days + 1), freq='h', tz='UTC', inclusive='left')
    return all((c, t) in seen and grid.isin(fetcher.valid_existing(c, t)).all()
               for c, t in fetcher.expected_scopes(config))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, required=True)
    args = parser.parse_args()
    print('true' if is_fresh(args.data_dir) else 'false')
