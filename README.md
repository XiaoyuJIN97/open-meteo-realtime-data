# Open-Meteo Realtime Data

Hourly 4-point Open-Meteo weather data for real-time load and renewable forecasting.

This repository is designed to be consumed by the Streamlit forecasting dashboard without making long Open-Meteo context calls at forecast time. It stores model-ready wide CSV files with one row per UTC timestamp.

## Layout

```text
data/
  raw/{country}/{target}/{year}.csv
  updates/{country}/{target}/{year}/{month}/{run_id}.csv
  update_manifest.csv
config/
  selected_weather_points_for_tp_v01_4p.csv
  selected_load_weather_points_for_tp_v01_4p.csv
scripts/
  fetch_open_meteo.py
```

Targets are `load`, `solar`, `onshore`, and `offshore`. Countries currently default to `BE`, `FR`, and `DE`.

## CSV Schema

Raw yearly files and update files use the same model-ready schema:

```text
timestamp_utc
country
target
temperature_2m_p1 ... temperature_2m_p4
relative_humidity_2m_p1 ... relative_humidity_2m_p4
shortwave_radiation_p1 ... shortwave_radiation_p4
wind_speed_100m_ms_p1 ... wind_speed_100m_ms_p4
wind_dir_sin_p1 ... wind_dir_sin_p4
wind_dir_cos_p1 ... wind_dir_cos_p4
deg_proxy
source
updated_at_utc
```

## Run Locally

```bash
python -m pip install -r requirements.txt
python scripts/fetch_open_meteo.py --start-date 2026-05-01 --end-date 2026-08-09
```

For the daily workflow, fetch a rolling context plus forecast window:

```bash
python scripts/fetch_open_meteo.py --rolling-days 100 --forecast-days 3
```

## GitHub Actions

`.github/workflows/fetch-open-meteo.yml` runs daily and commits updated files to the `data` branch. The dashboard should read from that branch, just like the ENTSO-E realtime data repo.

## Collection recovery and freshness

The workflow runs code from `main` and checks out `data` separately under
`archive/`. Code fixes therefore take effect without merging source into the
historical data branch. The daily target is 16:30 Europe/Brussels; delayed
scheduled attempts remain eligible later that evening. A green skipped job does
not indicate new data: check `data/status.json`, the workflow summary and the
manifest retrieval times.

Collection refreshes current/future weather for every configured series **before**
recovering history. Each successful series/chunk is saved independently and
published even when another request fails. The job still reports failure if any
required coverage is missing. Later attempts resume missing history in three-day
chunks, batching four locations per HTTP request. Existing valid historical rows
are preserved; raw CSVs and the manifest are replaced atomically.

A complete daily archive covers the preceding 100 UTC days plus today and three
following days. The daily skip check requires both full numeric hourly coverage
and current forecasts retrieved after 16:30 Brussels that day. A morning manual
recovery does not suppress the evening refresh.

Run a manual recovery with the **Fetch Open-Meteo Weather Data** workflow on
`main`, leaving dates blank and phase `all`. Phases `forecast` and `history` are
available separately. Explicit start/end dates are for bounded recovery. Locally:

```bash
python scripts/fetch_open_meteo.py --data-dir archive/data --phase all \
  --rolling-days 100 --forecast-days 3 --chunk-days 3
python -m scripts.check_freshness --data-dir archive/data
```

The manifest records endpoint, collection time, phase, model-selection policy and
vintage. The requested model policy remains Open-Meteo automatic best match;
this field does not identify a resolved model run. Recovered historical-forecast
values are tagged `retrospective_historical_forecast`: they are forecast API
archives, not weather observations and not certified issue-time vintages.
Original point locations and weather-variable definitions are unchanged.

Requests share a persistent HTTP session. A GitHub-hosted diagnostic across all
31 scopes observed 21/31 successful ordinary connections versus 31/31 successful
pooled requests. Stalled multi-day batches are retried as daily windows, then
individual locations, with exact coverage validation retained.
