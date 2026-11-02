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
| `adjust` | Map the start tables onto the history table: hindcasts year by year, operational starts with a saved mapping |
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
month of starts per array task, 36 tasks from `FIRST_MONTH` (default 2023-01).
Submit it from the repository root with the Python to use as `PYTHON`:

```bash
sbatch --export=ALL -A ACCOUNT -p PARTITION forecast/scripts/aggregate_hindcast.sbatch aifs ARCHIVE CATCHMENTS OUTPUT [FIRST_MONTH]
```

A member of about 1.3 GB (AIFS) or 2.3 GB (WeatherNext 2) takes 2-5 s on the
cluster, in under 200 MB of memory; a start of 21 members about a minute and
a half.

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
| `tmean_degC`, `tmax_degC`, `tmin_degC` | ✓ | ✓ |
| `u_wind_m_s`, `v_wind_m_s`, `wind_speed_m_s` | ✓ | ✓ |
| `relative_humidity_pct`, `vapor_pressure_deficit_kPa` | ✓ from 2d | – |
| `shortwave_down_mean_W_m2` | ✓ | – |
| `pet_hargreaves_mm_day` | ✓ | ✓ |

To see how far the raw forecasts are from the history before any adjustment,
compare their ensemble means with the history table, each system on its own:

```bash
python forecast/scripts/compare_history.py OUTPUT/aifs OUTPUT/wn2 --history DELIVERY.parquet --lead 1 5 10
```

## Adjust to the history table

The inflow model is trained on the history table, so the forecasts are mapped
onto it before use:

```bash
python -m catchment_forecast.adjust hindcast --forecast OUTPUT/aifs --history DELIVERY.parquet \
    --geojson CATCHMENTS --output OUTPUT/aifs_adjusted
```

Each column is quantile-mapped onto the history values of the same dates,
per catchment, lead group (days 1, 2, 3, 4-5, 6-7, 8-10, 11-14, 15 on) and
calendar month of the valid date with the months either side. The method is the
one `debias` uses to map ERA5-Land onto AORC, and it reuses that code. The fit
pools the members, and each member is mapped on its own. Precipitation is then
split into rain and snow by each catchment's snow-fraction curve, fitted on the
history table, at the mapped mean temperature. The temperatures are put in
order and PET is recalculated from them.

`hindcast` maps each year's starts with a fit on the other years. The adjusted
hindcasts therefore never see their own weather, as operational forecasts
never do. It writes one table per start with rainfall and snowfall added, and
`check.csv` with the bias, mean absolute error of the ensemble mean and CRPS of
the raw and adjusted forecasts against the history, per column, lead day and
catchment. It then fits all years together into `mapping.parquet`.
Operational starts go through the same mapping and the same code:

```bash
python -m catchment_forecast.adjust apply --mapping OUTPUT/aifs_adjusted/mapping.parquet \
    --forecast START.parquet --output DIR
```

A mapping holds for the model version and the lead days of the hindcast it
was fitted on. A new model version needs new hindcasts and a new fit. Lead days
beyond the hindcast take the mapping of the last lead group. All AIFS starts
(about 6 million rows) take about 2.5 minutes and 6 GB of memory.

## Download IFS ENS from TIGGE

```bash
python -m catchment_forecast.tigge --year 2024 --output DIR
```

The starts match the AI hindcasts (00 UTC on days 1, 5, ..., 29). The Data
Store queues one request per user at a time; in October 2026 a request for one
start's 50 members waited and ran 3-6 hours.
