# catchment-forecast

Daily catchment means of ensemble forecasts and hindcasts, on the same
polygons, UTC days and column names as the history tables of
`catchment-history` (`../history`), which this package uses for area
weights and meteorological formulas.

Every member is its own series: a table has one row per start, lead day,
member and catchment, and nothing depends on how many members a system has.

| Module | What it does |
| --- | --- |
| `aggregate` | AIFS-ENS, WeatherNext 2, GEFS, IFS ENS, CFSv2, EC46 and SEAS5 GRIB files to one Parquet table per start |
| `adjust` | Map the start tables onto the history table: hindcasts year by year, operational starts with a saved mapping |
| `complete` | Make every other history column for each member from its own adjusted weather: a degree-day snowpack, regressions and persistence fitted on the history |
| `gefs` | Download GEFS (control and 30 members) from NOAA's archive on AWS, only the fields used, and aggregate each start |
| `tigge` | Download IFS ENS (control and 50 members) from TIGGE on the ECMWF Data Store |
| `ifs` | Download IFS ENS from ECMWF's open data, the weather fields only, and aggregate each start |
| `cfs` | Download CFSv2 runs (member 01, to six months) from NOAA's archive on AWS and aggregate each |
| `s2s` | Download ECMWF's extended range (EC46) and its reforecasts from the S2S archive on the ECMWF Data Store |
| `seas5` | Download SEAS5 starts from the Copernicus Climate Data Store |
| `extended` | The extended-range and seasonal table of a day from the latest run of each system |

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
units stop the run. Rainfall and snowfall come from the adjustment against the
history tables.

| Column | AIFS-ENS | WeatherNext 2 | GEFS |
| --- | --- | --- | --- |
| `precipitation_mm` | ✓ accumulated, differenced | ✓ 6 h amounts, summed | ✓ 6 h amounts, summed |
| `tmean_degC`, `tmax_degC`, `tmin_degC` | ✓ | ✓ | ✓ |
| `u_wind_m_s`, `v_wind_m_s`, `wind_speed_m_s` | ✓ | ✓ | ✓ |
| `relative_humidity_pct`, `vapor_pressure_deficit_kPa` | ✓ from 2d | – | ✓ from 2 m dew point |
| `shortwave_down_mean_W_m2` | ✓ | – | ✓ DSWRF |
| `pet_hargreaves_mm_day` | ✓ | ✓ | ✓ |
| `net_radiation_mean_W_m2` | – | – | ✓ DSWRF − USWRF + DLWRF − ULWRF |
| `cloud_cover_fraction` | – | – | ✓ TCDC / 100 |
| `actual_evapotranspiration_mm` | – | – | ✓ LHTFL × 86400 / 2.45e6 |
| `snow_water_equivalent_mm`, `snow_depth_m` | – | – | ✓ WEASD, SNOD, without glacier cells |
| `snowmelt_mm` | – | – | ✓ SNOHF × 86400 / 3.34e5 |
| `freezing_level_above_ground_m`, `freezing_level_above_sea_level_m` | – | – | ✓ HGT at 0 °C, HGT of the surface |
| `soil_moisture_layer1_m3_m3`, `root_zone_soil_moisture_0_100cm_m3_m3`, `soil_moisture_layer4_m3_m3` | – | – | ✓ SOILW 0-10 cm; 0-10, 10-40, 40-100 cm weighted 0.1/0.3/0.6; 1-2 m |
| `soil_temperature_layer1_degC` to `layer4_degC` | – | – | ✓ TSOIL 0-10, 10-40, 40-100, 100-200 cm |

The GEFS soil layers are not those of ERA5-Land (0-7, 7-28, 28-100,
100-289 cm); the adjustment maps each onto its history column. A GEFS member
fetched with `gefs --weather` holds the fields of the first four rows only, and
its table has those columns.

To see how far the raw forecasts are from the history before any adjustment,
compare their ensemble means with the history table, each system on its own:

```bash
python forecast/scripts/compare_history.py OUTPUT/aifs OUTPUT/wn2 --history DELIVERY.parquet --lead 1 5 10
```

## Download and aggregate GEFS

```bash
python -m catchment_forecast.gefs --start 2024-01-01 --end 2024-01-31 --archive ARCHIVE \
    --geojson CATCHMENTS --output OUTPUT --delete
```

GEFS v12 runs a control and 30 members at 00 UTC to 35 days on a 0.5 degree
grid. NOAA's archive on AWS keeps every run, with the same fields from 2023 on,
and has each new run a few hours after its start, so it serves both as the
hindcast and as the operational source. The GRIB index files tell where each
field lies, and only the fields used are fetched: about 320 MB a member for 22
days at 6 h steps, which took 12-20 s to download and 4-5 s to aggregate. Each
start goes into `OUTPUT/gefs` as soon as it is down, and `--delete` then removes
its GRIB files. The starts are those of the AI hindcasts; `--every-day` takes
every day, as operations will. `--hours 24` fetches only the first day:
`--every-day --members 3 --hours 24` is about 50 MB a day and gives the
lead-day-1 tables that bridge the days between the end of the history table
and a forecast start. On a Slurm cluster whose compute nodes reach AWS,
`scripts/gefs_hindcast.sbatch` runs one month per array task:

```bash
DELETE=1 sbatch --export=ALL -A ACCOUNT -p PARTITION forecast/scripts/gefs_hindcast.sbatch ARCHIVE CATCHMENTS OUTPUT
```

2023-2025 is 285 starts of 31 members, about 2.8 TB to download. `--weather`
fetches only the fields the product takes from a system's own weather (2 m
temperature and dew point, 10 m wind, precipitation, downward shortwave), about
a quarter of the bytes, and the tables hold the weather columns only. That is
what a GEFS hindcast needs as a third ensemble beside the AI systems: the
control and 10 members with `--weather` is about 1 GB a start and 0.3 TB in
all, and `MEMBERS=11 WEATHER=1 DELETE=1` passes this to `gefs_hindcast.sbatch`.

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
(about 6 million rows, 10 columns) took 5.5 minutes and 6 GB of memory on the
cluster; GEFS, with 31 members and 25 columns, has about 9 million rows.

## Complete into the product

```bash
python -m catchment_forecast.complete fit --history DELIVERY.parquet --before 2023 --output OUTPUT/completion.json
python -m catchment_forecast.complete run --fit OUTPUT/completion.json --history DELIVERY.parquet \
    --forecast OUTPUT/aifs_adjusted OUTPUT/wn2_adjusted --gap OUTPUT/gefs_day1_adjusted --output OUTPUT/product
```

The inflow model takes every column of the history table, and the AI systems
forecast only the weather. `complete` makes the other columns for each member
from its own adjusted weather, as a hydrological model would, starting from
the last day the history table has: the anchor day, 6 days before the start,
which is how late the history runs in operations.

| Columns | How |
| --- | --- |
| SWE, snowmelt | A degree-day snowpack from the history SWE on the anchor day, driven by the member's snowfall and mean temperature; melt factor and threshold fitted per catchment on the history over 27-day runs |
| Humidity and shortwave where a system lacks them, net radiation, cloud, AET, the freezing levels, soil temperature layer 1, snow depth | Linear regressions per catchment and calendar month (with its neighbours) on the member's weather and SWE, fitted on the history; a system's own humidity and shortwave are predictors where it has them |
| Soil moisture, soil temperature layers 2-4 | The history value on the anchor day plus the change of the day-of-year climatology |

The five days between the anchor and the start come from the first lead day of
GEFS's run of each day (`gefs --every-day --hours 24`, adjusted like any
system), completed the same way and shared by every member. Each member is then
one series from lead day -4 to its last lead day, and a start's table holds
every system, with a `system` column. `fit` uses only history years before
`--before`, so the hindcast product never sees its own years; operations use a
fit on every year (`fit` without `--before`), as their mappings use every
hindcast year. For the years from `--before`, `fit` prints how close the snow
model comes to the history SWE beside persistence (the anchor SWE plus the
change of its climatology), and the bias and mean absolute error of each
regression fed the history's own weather, which separates the completion's
error from the forecasts'. `run` writes `check.csv` against the history where
it has the dates. On the cluster, once the start tables are there,
`scripts/product_hindcast.sbatch HISTORY CATCHMENTS OUTPUT` runs the
adjustments (`OUTPUT/gefs` too, where it exists), the hindcast fit and
completion, and the operational fit in one job.

`complete run --overwrite` rebuilds existing products through a temporary file,
replacing each one only after its new table is written. The daily job uses this
when a later system arrives. Without the flag, existing products are skipped.

## Operations

```bash
sbatch --export=ALL -A ACCOUNT -p PARTITION forecast/scripts/daily_product.sbatch HISTORY CATCHMENTS HINDCAST AIFS_ROOT WN2_ROOT OPS [DATE]
```

One job makes the product of the 00 UTC start of `DATE` (default today) from
the same code and files as the hindcast: `HINDCAST` is the folder of
`product_hindcast.sbatch`, with the three mappings and `completion_ops.json`;
`AIFS_ROOT` and `WN2_ROOT` hold the operational runs as start folders
`YYYYMMDDHH`, the member files in the folder or one level down. The job fetches
the GEFS day-1 tables of the five days before the start that are not under
`OPS` yet, aggregates and maps each system's run that is in, and completes
them into `OPS/product/DATE00.parquet`. It needs the history table to reach six
days before the start (`history/scripts/daily_update.sh`) and should run once
the runs are in; run it again later and the product is remade with the
systems that have arrived since. The member count does not matter: 51 AIFS
members go through the mapping fitted on 21. When `HINDCAST` has
`gefs_adjusted`, the job also fetches the start's GEFS run from AWS
(`GEFS_MEMBERS` members, default 11, weather fields only, to `GEFS_HOURS`,
default 528) and maps and completes it like the others. The GEFS 00 UTC run
reaches 16 days about 06:30 UTC and 22 days only the next day, so a morning
run sets `GEFS_HOURS=384`; `GEFS_SOURCE` reads GEFS from another copy with the
same layout, NCEP's own server or a folder on disk.

```bash
sbatch -A ACCOUNT -p PARTITION forecast/scripts/daily_ops.sh CONFIG
```

runs the whole day: the history update to six days before today, then the
product of today, and prints each system's members, lead days and blank
values. `CONFIG` is a file of settings kept outside the repository (listed at
the top of the script). Run it from `scrontab` once the runs are in, more than
once a morning if wanted: a run that finds another going stops, and a later
one finds the history up to date and remakes the product with what has
arrived since.

## IFS ENS from ECMWF's open data

```bash
python -m catchment_forecast.ifs --start 2024-06-01 --end 2025-12-31 --members 11 --archive ARCHIVE \
    --geojson CATCHMENTS --output OUTPUT --delete
```

ECMWF's open data hold the 00 UTC ENS (control and 50 members, 0.25°) to
360 h, one file per step with a JSON index. Only the fields of a system's own
weather in the product are fetched (2 m temperature and dew point and 10 m wind
every 6 h, precipitation and downward shortwave at the end of each day), about
145 MB a member, from Google's copy of the open data, which keeps every day from
mid 2023; these fields are there from June 2024. IFS is then a fourth system of
the product, mapped on its hindcast and completed like the AI systems:
`product_hindcast.sbatch` takes `OUT/ifs` when it is there, and
`daily_product.sbatch` fetches the start's run when the hindcast has
`ifs_adjusted`. The run is complete about 08:00 UTC; until its last step is in,
the product leaves it out.

## The extended-range and seasonal table

Beyond the product's 15 to 22 days, each day also gets a table of the latest
run of four systems, each mapped onto the history with a mapping fitted on its
own hindcast (`adjust hindcast --lead-groups` with `EXTENDED_GROUPS`: weeks to
day 46, then months), with the twelve columns of `extended.COLUMNS`:

| System | Source | Run used on day D | Members | Days | Hindcast for the mapping |
| --- | --- | --- | --- | --- | --- |
| `ec46` | ECMWF extended range, S2S archive (CC BY 4.0, open 48 h after the start) | D - 2 | 101 | 46 | reforecasts of 20 model dates of 2026 over 2006-2025, 11 members |
| `gefs` | GEFS on AWS, `gefs --extended` | D - 1 (35 days only the next day) | 31 | 35 | AI hindcast starts of 2023-2025, 11 members |
| `cfs` | CFSv2 time series on AWS | the four runs of each of the last 7 days | 28 | 183 | 00 UTC runs on the AI hindcast days from May 2023 |
| `seas5` | SEAS5, Copernicus Climate Data Store | the latest month released before D at 00 UTC | 51 | 184 | starts of 2006-2025, 11 members |

Each system brings its own fields for all twelve columns: tmax and tmin from its
own extremes (6 h for CFSv2 and EC46, 24 h for SEAS5), net radiation from its
net shortwave and longwave, the top soil layer (0-10 cm in CFSv2, 0-20 cm in
EC46, 0-7 cm in SEAS5) mapped onto the history's. EC46 gives daily means of
temperature and dew point, and 10 m wind at 00 UTC. A CFSv2 run from 06, 12 or
18 UTC starts its lead days at the next whole UTC day.

```bash
sbatch --export=ALL --array=0-31%2 -A ACCOUNT -p PARTITION forecast/scripts/hindcast_tables.sbatch cfs ARCHIVE CATCHMENTS OUT 2023-05
sbatch --export=ALL -A ACCOUNT -p PARTITION forecast/scripts/extended_hindcast.sbatch HISTORY CATCHMENTS OUT
sbatch --export=ALL -A ACCOUNT -p PARTITION forecast/scripts/daily_extended.sbatch CATCHMENTS HINDCAST OPS [DATE]
```

`hindcast_tables.sbatch` makes the start tables of `ifs`, `cfs` and `gefs35`,
one month per array task. `extended_hindcast.sbatch` downloads the EC46
reforecasts and the SEAS5 starts (the Data Stores need the account's key in
`~/.cdsapirc` and the S2S and seasonal licences accepted) and fits the mapping
of each extended system whose tables are there. `daily_extended.sbatch` makes
`OPS/extended/DATE.parquet`, and `daily_ops.sh` runs it after the product once
any of these mappings exists.

The seasonal run is selected using what was released before `DATE` at 00 UTC.
The CDS releases ECMWF seasonal data on the 6th at 12 UTC, so dates through
the 6th use the preceding month. A historical rebuild also excludes newer
monthly starts already cached on disk. See the [CDS dataset description](https://cds.climate.copernicus.eu/datasets/seasonal-original-single-levels?tab=overview).

Use a separate raw archive for each system and field set. `--delete` removes
a downloaded start only after its Parquet table is written. IFS records a
completed member set before aggregation; an interrupted download without that
record is fetched again. EC46 retains its raw response if aggregation fails.
Keep the small Parquet tables and fitted mappings; reruns reuse them.

For date-parallel operational backfills, prepare the shared GEFS day-1 tables
and their adjusted tables once before submitting the date array. Each date
then writes its own product; avoid two jobs for the same date. Reserve a new
product directory when adding IFS to an existing hindcast: existing hindcast
products are skipped unless explicitly rebuilt with `complete run --overwrite`.

## Download IFS ENS from TIGGE

```bash
python -m catchment_forecast.tigge --year 2024 --output DIR
```

The starts match the AI hindcasts (00 UTC on days 1, 5, ..., 29). The Data
Store queues one request per user at a time; in October 2026 a request for one
start's 50 members waited and ran 3-6 hours.
