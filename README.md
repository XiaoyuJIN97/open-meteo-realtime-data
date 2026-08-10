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

For the daily workflow, refresh a small overlap plus the forecast window. The longer model context is read from the accumulated raw archive:

```bash
python scripts/fetch_open_meteo.py --rolling-days 14 --forecast-days 3
```

## GitHub Actions

`.github/workflows/fetch-open-meteo.yml` runs daily and commits updated files to the `data` branch. The dashboard should read from that branch, just like the ENTSO-E realtime data repo.
