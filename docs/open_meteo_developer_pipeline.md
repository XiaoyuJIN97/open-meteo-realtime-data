# Open-Meteo Weather Data Developer Pipeline

This document introduces the Open-Meteo data fetching code used by the TP++
real-time load and renewables forecasting dashboard. The purpose of the weather
pipeline is to maintain a ready-to-read four-point weather archive so the online
forecasting dashboard does not need to make long Open-Meteo context calls during
the daily forecast run.

## Repository Role

The Open-Meteo weather archive is maintained in:

```text
XiaoyuJIN97/open-meteo-realtime-data
```

The real-time forecasting dashboard consumes the repository's `data` branch as
its weather covariate source. The data are prepared as model-ready wide hourly
CSV files with one row per UTC timestamp.

Current scope:

- Countries: `BE`, `FR`, `DE`
- Targets: `load`, `solar`, `onshore`, `offshore`
- Weather points: four selected points per country/target
- Resolution: hourly
- Primary timestamp convention: UTC

## Main Entry Point

The main fetching script is:

```text
scripts/fetch_open_meteo.py
```

It is responsible for:

- reading country, target, variable, and weather-point configuration
- calling Open-Meteo forecast and historical forecast APIs
- standardizing timestamps to UTC
- converting wind direction to sine/cosine covariates
- deriving the load degree proxy
- writing yearly raw files and per-run update snapshots
- maintaining `data/update_manifest.csv`

Typical local commands:

```bash
python -m pip install -r requirements.txt
python scripts/fetch_open_meteo.py --start-date 2026-05-01 --end-date 2026-08-09
python scripts/fetch_open_meteo.py --rolling-days 14 --forecast-days 3
```

The first command is useful for historical/backfill ranges. The second command is
the normal daily operational mode.

## Configuration Files

The Open-Meteo API and target setup are configured in:

```text
config/targets.yml
```

Current Open-Meteo variables:

```text
temperature_2m
relative_humidity_2m
shortwave_radiation
wind_speed_100m
wind_direction_100m
```

Selected weather points are stored in:

```text
config/selected_weather_points_for_tp_v01_4p.csv
config/selected_load_weather_points_for_tp_v01_4p.csv
```

Load has its own selected-point file because the optimal load weather points can
differ from the renewable generation points.

## Open-Meteo API Usage

The script uses two Open-Meteo endpoints:

```text
https://api.open-meteo.com/v1/forecast
https://historical-forecast-api.open-meteo.com/v1/forecast
```

The fetch logic splits the requested range into:

- historical/recent days before today, queried from the historical forecast API
- today and future days, queried from the forecast API

Large requests are chunked by `chunk_days` from `config/targets.yml`. If a chunk
fails, the script falls back to one-day requests so one bad day does not
necessarily fail the whole collection.

## Daily Fetch Window

The daily workflow uses:

```bash
python scripts/fetch_open_meteo.py --rolling-days 14 --forecast-days 3
```

This means each daily run refreshes:

- the most recent 14 days, to capture revised recent weather values and fill any
  gaps
- the next 3 forecast days, so the dashboard has weather covariates ready for
  the day-ahead forecast horizon

The longer 3-month model context is not fetched from Open-Meteo during the
dashboard forecast. It is read from the accumulated weather archive.

## GitHub Actions Schedule

The scheduled collector workflow is:

```text
.github/workflows/fetch-open-meteo.yml
```

The workflow checks out the `data` branch, runs the fetcher, commits changed data
files, and pushes back to the `data` branch.

The collection should finish before the real-time forecasting dashboard starts
its daily forecast around 18:30 Europe/Brussels. Since Open-Meteo does not need
to wait for ENTSO-E's 18:00 TSO forecast publication, the Open-Meteo job should
run earlier, around 16:30 Europe/Brussels.

Because GitHub cron is UTC-only and Europe/Brussels switches between CET and
CEST, the workflow should use UTC cron trigger(s) plus a Brussels-time guard when
year-round local timing matters.

## Stored Data Layout

The stored data layout is:

```text
data/
  raw/{country}/{target}/{year}.csv
  updates/{country}/{target}/{year}/{month}/{run_id}.csv
  update_manifest.csv
```

The yearly files under `data/raw` are the accumulated model-ready archive. The
per-run files under `data/updates` are useful for auditing, debugging, and
recovering recent fetches. The manifest records which country/target windows were
updated by each run.

## Output Schema

Raw yearly files and update files use the same wide schema:

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

There is one row per UTC delivery timestamp. Point suffixes `p1` to `p4`
correspond to the four selected weather points for the country and target.

## Feature Engineering

### Wind Direction

Open-Meteo returns `wind_direction_100m` in degrees. The script converts this
circular variable into sine and cosine features:

```text
wind_dir_sin_p{point} = sin(wind_direction_100m)
wind_dir_cos_p{point} = cos(wind_direction_100m)
```

This avoids artificial discontinuities at 0/360 degrees.

### Degree Proxy

For load forecasting, the script derives:

```text
deg_proxy = abs(mean(temperature_2m_p1 ... temperature_2m_p4) - 18.0)
```

This single covariate summarizes heating/cooling stress relative to an 18 C
comfort/reference temperature.

## Timestamp Convention

All stored timestamps use UTC:

```text
timestamp_utc
updated_at_utc
```

UTC is used because it avoids daylight-saving ambiguity. Europe/Brussels local
time switches between CET and CEST, which can create repeated or missing local
hours. Downstream dashboards convert UTC timestamps to Europe/Brussels only for
display.

## Dashboard Integration

The real-time forecasting dashboard reads this archive as its weather source.
For each forecast run, it aligns weather rows to the ENTSO-E timestamp grid and
uses the selected target-specific covariates:

- Load: selected engineering covariate by country/model, such as temperature,
  humidity, shortwave radiation, or `deg_proxy`
- Solar: four-point `shortwave_radiation` and `temperature_2m`
- Wind: four-point `wind_speed_100m_ms`, `wind_dir_sin`, and `wind_dir_cos`

Weather features are combined with the latest TSO forecast covariate inside the
forecasting dashboard.

## Operational Checks

Developers should periodically verify:

- GitHub Actions runs are completing before the dashboard forecast window
- `data/update_manifest.csv` contains recent entries for all countries/targets
- yearly raw files have continuous hourly coverage
- future forecast coverage extends at least through the next day-ahead horizon
- no target has missing selected weather points
- units remain consistent, especially `wind_speed_100m_ms`
- `deg_proxy` is present when temperature columns are available

Useful checks:

```bash
git log --oneline -- data/update_manifest.csv
python scripts/fetch_open_meteo.py --rolling-days 14 --forecast-days 3
```

## Adding Countries Or Targets

To add a new country:

1. Add the country code to `config/targets.yml`.
2. Add four selected points for every required target.
3. Run a short local fetch for that country.
4. Verify yearly raw files and update snapshots.
5. Update the downstream dashboard country configuration.

To add a new target:

1. Add the target to `config/targets.yml`.
2. Add selected points with the matching `type`.
3. Confirm required variables and feature engineering.
4. Run a one-day test fetch.
5. Update downstream dashboard covariate mappings.

## Relationship To ENTSO-E Data

This weather archive is separate from the ENTSO-E realtime data archive.
ENTSO-E provides TSO forecasts and realized actuals. Open-Meteo provides weather
covariates. The forecasting dashboard joins both sources by UTC delivery
timestamp when preparing daily online forecasts.
