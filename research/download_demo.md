# Hourly extraction and daily aggregation

For the full AORC diagnostics, authenticated CDS products and final wide
CSV/Parquet export, use the [data pipeline guide](../docs/data_pipeline.md).
The examples and recorded runs below document the earlier base-field and
public-mirror validations.

These research scripts extract historical gridded estimates over WGS84
catchments. They do not provide sensor-only observations or demonstrate
hydrologic forecast skill. The requested window is **1996-01-01 through
2025-12-31**: 10,958 Gregorian dates, including leap days.

The current `outlet_cell` boundaries contain **43 independent local drainage
domains**, with no shared project polygons. Bounded December 29–31, 2025
replays completed on 2026-10-06:

- ERA5-Land: all 43 domains, 9,417 hourly rows and 645 valid daily statistics
  out of 645, saved under `outputs/era5_land_demo_43_independent`. Verified
  cached responses covered the required region; request URLs and response
  hashes were retained and polygon weights were recomputed. No new network
  bytes were downloaded.
- AORC: all 43 domains and all eight variables were reprocessed on the
  compute host from verified cached chunks, producing 1,548 daily rows under
  `data/aorc_demo_43_independent`: 1,505 valid statistics and 43 blank December
  31 precipitation totals with 23 of 24 hours, because the required 2026
  boundary archive is absent. Each formerly shared pair differs in all 35
  paired valid daily statistics. Geometry and output hashes, 43 distinct
  forcing groups, and retained routing metadata/warnings were verified.
  No new network bytes were downloaded.

The earlier **whole-unit geometry** demo covered all 43 projects and returned
1,505 valid AORC statistics out of 1,548, plus 645 valid ERA5-Land statistics.
An intermediate D8 demo used 41 forcing groups for 43 project rows; both
shared pairs had identical weather values. The separate Mica/Oxbow offline
check returned 70 valid AORC statistics out of 72. These earlier results
remain evidence of retrieval and aggregation, but their polygon means do
not validate the current independent domains. Rebuild weights and write to
a new output directory whenever geometry changes.

An earlier independent comparison of Mica/Oxbow AORC against native NOAA
P/T/q/p on December 31 at 22Z and 23Z found zero raw-value or mask differences
in 331,296 cell comparisons; all 16 polygon means agreed within CSV rounding.
That check used the earlier geometry. These samples test retrieval and
processing, not 30 years of meteorological quality or model performance.

## Install

Use Python 3.10 or newer, install the project and research requirements, and
install the `zstd` command-line decoder. The public ERA5-Land demo also uses
`curl`.

```sh
python -m pip install -e . -r research/requirements.txt
```

## AORC

Build the corrected geometry first, or substitute the path of your newly
built D8 file in the commands below:

```sh
hydro-map build configs/columbia.yaml --cache-dir data \
  --output outputs/projects43_independent/dam_catchments.geojson
```

On the compute host, place that GeoJSON under `inputs/projects43_independent`.
The bounded replay below uses an existing verified source cache; omit
`--cache-only` to permit downloads within the specified transfer ceiling.

```sh
python research/download_aorc.py \
  --geojson inputs/projects43_independent/dam_catchments.geojson \
  --output-dir data/aorc_demo_43_independent --cache-dir cache/aorc \
  --start 2025-12-29 --end 2025-12-31 \
  --cache-only --max-download-gb 1 --keep-chunks
```

All features are used by default; add `--projects MICA OXBOW` for a subset.
Input features require unique string
`properties.id` values and a consistent `properties.part` of `local` or `total`.
Invalid geometries and incomplete grid coverage are rejected. Weights are
fractional intersections of native cells and polygons in the equal-area
EPSG:6933 projection. Longitude/latitude are never treated as planar area.
The input geometry hash and exact request hashes are recorded.

Each current project has its own `forcing_group`. Kootenay Canal's polygon
is the natural river reach at the tailrace, including lateral runoff to the
bypassed river. It does not define exclusive turbine inflow. `run.json`
retains `catchment_role: natural_reach_at_tailrace`,
`diversion_intake_project: CORRA_LINN` and `routing_requires_operations: true`.
Diversion, bypass, spill and storage allocation require operating data and
a separate routing model. The download plan and ERA5-Land summary retain
the same interpretation.

Other configurations may still declare shared groups. Their members must
have identical geometry and catchment part, and each group must be counted
once in an area or water-volume balance. Unmarked equal geometries are not
automatically assigned a shared group.

For all 43 domains and 30 years, use a compute host with reliable network
access. Source reads are potentially TB-scale even though the output tables
are much smaller. A 2,000 GB transfer ceiling is an upper limit, not an estimate
of the downloaded table size. For full-period extraction, use:

```sh
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python research/download_aorc.py \
  --geojson inputs/projects43_independent/dam_catchments.geojson \
  --output-dir data/aorc_1996_2025_projects43_independent --cache-dir cache/aorc \
  --start 1996-01-01 --end 2025-12-31 --workers 4 \
  --max-download-gb 2000
```

The downloader retains only small coordinates/metadata and request logs by
default; meteorological chunks are discarded after aggregation. It holds one
year's polygon series in memory and writes:

- `hourly_YEAR.csv.gz`: sorted hourly series for every source/project/variable.
- `daily_YEAR.csv.gz`: daily statistics, including explicit missing values.
- `year_YEAR.json`: output hashes and completeness per hourly series.
- `run.json`: geometry, date, variable, and code identity.

Annual outputs are atomic and hash-verified before reuse. Restart the same
command to skip previously computed years; an interrupted year is recomputed.
Changing the geometry, dates, variables or processing code requires a new
output directory. A computed year can still contain data gaps: inspect its
`status`, hourly quality records and daily `qc` column. To retry incomplete
years after a source correction, repeat the command with
`--refresh-incomplete`; this bypasses cached objects for the affected years.
It is not an automatic source-version monitor. Use a new output directory to
audit changes to years previously classified as complete.

Every raw missing code is masked before applying packing scales. A partial
catchment remains missing; its remaining cells are never renormalized to stand
in for the whole basin. Broad physical guards catch invalid values without
clipping them. Retain and investigate the quality flags before modeling.

### The last precipitation day

AORC has exact hourly coordinates through **2025-12-31 23Z**. A UTC daily total
requires the amounts ending at 01Z through the following 00Z. The public
2026 Zarr store and January 2026 native NWRFC archive were unavailable on
2026-10-06. December's native last-hour NetCDF files and the complete December
NWRFC 4-km precipitation ZIP inventory also end at 23Z.

Consequently, **2025-12-31 precipitation is blank with 23 valid hours**, while
the other seven AORC fields can cover that date. The requested calendar is
preserved. Do not call a 23-hour sum a daily total or splice one ERA5-Land hour
into AORC without a separately evaluated method.

Sources: [NOAA AORC distribution](https://hydrology.nws.noaa.gov/pub/AORC/V1.1/),
[public Zarr archive](https://noaa-nws-aorc-v1-1-1km.s3.amazonaws.com/),
[AORC methods](https://hydrology.nws.noaa.gov/pub/AORC/V1.1/Documents/AORC-Version1.1-SourcesMethodsandVerifications.pdf).

## ERA5-Land comparison demo

```sh
python research/download_era5_land.py \
  --geojson outputs/projects43_independent/dam_catchments.geojson \
  --output outputs/era5_land_demo_43_independent \
  --start-date 2025-12-29 --end-date 2025-12-31

python research/aggregate_daily.py \
  --input outputs/era5_land_demo_43_independent/hourly.csv \
  --output outputs/era5_land_demo_43_independent/daily.csv \
  --start 2025-12-29 --end 2025-12-31
```

This small demo downloads native temperature, precipitation and SWE from the
public NCAR catalog, in short requests to avoid subset-service timeouts. It
validates returned units, grid, calendar, forecast/analysis type and overlap
consistency. The subset bounds are calculated from the input geometry, padded
and snapped to the source grid. It includes 2026-01-01 00Z, required for
December 31 precipitation.
An initial 00Z precipitation record lacks its previous hour and is explicitly
flagged; that record belongs to the day before the requested daily window.

ERA5-Land forecast precipitation is cumulative from the daily forecast start.
01Z is step 1; subsequent hourly amounts are differences, including the next
00Z final step. Summing raw cumulative values is incorrect. Negative increments
below -0.0001 mm are rejected; smaller negative numerical residuals become zero.

The [NCAR mirror](https://gdex.ucar.edu/datasets/d633008/) starts in November
2001 and does not include the needed soil fields. This script is a bounded
comparison demo, **not a 30-year ERA5-Land downloader**. Earlier years and soil
variables require the complete [CDS archive](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land?tab=overview),
accepted license terms and configured account credentials. That route remains
unverified until authenticated retrieval succeeds. Never store keys in this repo.

## Units and daily definitions

All outputs are polygon means or statistics of polygon means. They are not
point samples at the dam. `date` denotes a UTC calendar day.

| Hourly variable | Output unit | Daily output and operation |
|---|---|---|
| `precipitation_mm` | mm per hour interval | `precipitation_mm`: sum of 24 hour-ending amounts; daily total in mm |
| `temperature_c` | °C | `tmean_c`: mean; `tmin_c`, `tmax_c`: minimum/maximum of 24 hourly polygon means |
| `specific_humidity_kgkg` | kg/kg | Mean; this is specific humidity, not relative humidity |
| `surface_pressure_pa` | Pa | Mean; divide by 1,000 for kPa |
| `u_wind_ms`, `v_wind_ms` | m/s | Mean eastward and northward components at 10 m |
| `shortwave_down_wm2` | W/m² | `shortwave_down_mean_wm2`; also `shortwave_down_energy_mjm2` in MJ/m² |
| `longwave_down_wm2` | W/m² | `longwave_down_mean_wm2`; also `longwave_down_energy_mjm2` in MJ/m² |
| `snow_water_equivalent_mm` | mm water equivalent | Mean of hourly ERA5-Land SWE states; not snow depth or snowfall |

For AORC, kg/m² of liquid-water-equivalent precipitation is numerically mm;
do not multiply hourly amounts by 3,600. Temperature is converted from kelvin
by subtracting 273.15. ERA5-Land precipitation and SWE are converted from metres
of water equivalent to mm by multiplying by 1,000.

Radiation energy is the hourly flux sum multiplied by 0.0036. This is rectangular
numerical integration of hourly samples, not an independently observed daily
energy accumulation. Radiation is interpreted as instantaneous from the
[NLDAS forcing convention, Table 3](https://hydro1.gesdisc.eosdis.nasa.gov/data/NLDAS/NLDAS2_README.pdf)
and [AORC source processing](https://hydrology.nws.noaa.gov/pub/AORC/V1.1/Documents/Fall_et_al_2023_JAWRA.pdf).
AORC metadata does not explicitly specify radiation time bounds; GDAS fallback
timing has not been independently verified.

## Use in a hydrologic model

1. Join by `date`, `project_id` and `source`; keep different sources separate.
   Pivot `variable` to columns only after checking `qc == valid`, 24 valid hours
   and full valid-area coverage. Do not drop dates and shift later observations.
2. Start a precipitation–temperature model with AORC `precipitation_mm` and
   `tmean_c`. Use additional variables only when the chosen model's equations
   require them. These scripts do not derive PET, rain/snow partition, relative
   humidity, net radiation, freezing level or soil states.
3. Match the flow target's daily boundary before training. These UTC tables do
   not automatically match a local station day or an operational hydrologic day.
   Preserve hourly data when a different boundary may be required.
4. `tmin_c` and `tmax_c` are extrema of hourly catchment-mean temperature. They
   are **not** spatial means of native-cell daily extrema. Do not substitute
   them into a formula calibrated on the latter without validation. Likewise,
   the magnitude of mean wind components is not mean wind speed; derive nonlinear
   diagnostics at the required cell/time resolution before averaging.
5. Snow and soil states from reanalysis represent another model's estimates.
   Evaluate their usefulness independently; do not treat them as observed truth.
   Calibrate and test on separate chronological periods, with a model-appropriate
   warm-up period and a documented definition of the target flow.

Rainfall/snowfall are parts of total precipitation, whereas SWE is stored water.
Do not add SWE to daily precipitation. To convert a basin-average depth into a
volume, 1 mm over 1 km² is 1,000 m³; that water input is not immediate streamflow.

The daily aggregator is source-independent:

```sh
python research/aggregate_daily.py --input hourly.csv.gz --output daily.csv.gz \
  --start 1996-01-01 --end 2025-12-31
```

Input must be sorted by `(source, project_id, variable, time_utc)`. Each hourly
row includes `units`, `temporal_kind`, `valid_area_fraction` and `qc`. Duplicate
hours, unsupported units and changing temporal definitions fail explicitly.
Missing days are emitted with blank values, `expected_hours=24`, actual
`valid_hours` and a quality flag. For the eight AORC inputs, the 12 daily
statistics produce 5,654,328 long-format rows over 43 projects and 30 years;
the row count alone does not establish that the values are complete.
