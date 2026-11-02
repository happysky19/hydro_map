# catchment-forecast

Daily catchment means of ensemble forecasts and hindcasts, on the same
polygons, UTC days and column names as the history tables of
`catchment-history` (`../history`), which this package uses for area
weights and meteorological formulas.

Every member is its own series: a table has one row per start, lead day,
member and catchment, and nothing depends on how many members a system has.

| Module | What it does |
| --- | --- |
| `aggregate` | AIFS-ENS and WeatherNext 2 GRIB archives to one Parquet table per start |
| `tigge` | Download IFS ENS (control and 50 members) from TIGGE on the ECMWF Data Store |

## Aggregate a hindcast archive

```bash
export PYTHONPATH=forecast/src:history/src
python -m catchment_forecast.aggregate --system aifs --archive ARCHIVE --geojson CATCHMENTS \
    --output OUTPUT --start 2024-01-01 --end 2024-01-01
```

`ARCHIVE` holds one folder per start, `YYYYMMDDHH`, with one GRIB file per
member. Output goes to `OUTPUT/aifs/YYYYMMDDHH.parquet`; a start already
written is skipped. `CATCHMENTS` is the GeoJSON of every catchment, such as the
`_catchments.geojson` that `merge-deliveries` writes beside the delivered
history table. On a Slurm cluster, `scripts/aggregate_hindcast.sbatch` runs one
month of starts per array task (36 tasks from `FIRST_MONTH`, default 2023-01).
A member of about 1.3 GB (AIFS) or 2.3 GB (WeatherNext 2) takes a few seconds
plus the time to read it, in under 200 MB of memory.

Lead day d is hours 24(d-1) to 24d after the start. Precipitation is the 24 h
amount; temperatures, wind and humidity use the samples at 0, 6, 12 and 18 h of
the day, so tmax and tmin come from four samples (three on WeatherNext 2's
first day, which has no step 0). Amounts are converted from the units each
GRIB field declares (m or kg/m2 of precipitation, J/m2 of radiation); other
units stop the run. Rainfall, snowfall and the columns a system does not
forecast come from the adjustment against the history tables.

| Column | AIFS-ENS | WeatherNext 2 |
| --- | --- | --- |
| `precipitation_mm` | ✓ accumulated, differenced | ✓ 6 h amounts, summed |
| `tmean_c`, `tmax_c`, `tmin_c` | ✓ | ✓ |
| `u_wind_ms`, `v_wind_ms`, `wind_speed_ms` | ✓ | ✓ |
| `relative_humidity_pct`, `vapor_pressure_deficit_kpa` | ✓ from 2d | – |
| `shortwave_down_mean_wm2` | ✓ | – |
| `pet_hargreaves_mm` | ✓ | ✓ |

To see how far the raw forecasts are from the history before any adjustment,
compare their ensemble means with the history table:

```bash
python forecast/scripts/compare_history.py OUTPUT/aifs --history DELIVERY.parquet --lead 1 5 10
```

## Download IFS ENS from TIGGE

```bash
python -m catchment_forecast.tigge --year 2024 --output DIR
```

The starts match the AI hindcasts (00 UTC on days 1, 5, ..., 29). The Data
Store queues one request per user at a time; in October 2026 a request for one
start's 50 members waited and ran 3-6 hours.
