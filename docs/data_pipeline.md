# Historical catchment data pipeline

This guide covers the 1996-01-01 through 2025-12-31 daily data workflow for the
43 configured local catchments. It produces one row per project and UTC date,
with source-qualified variables, units and quality information. A complete
calendar contains 10,958 dates and 471,194 project-day rows. Missing values do
not remove dates from that calendar.

The boundaries use documented HydroSHEDS outlet cells and exclude virtual
endorheic connections. Kootenay Canal represents natural drainage at its
tailrace return. Its turbine inflow requires a separate operational diversion
model. See [project outlets](project_outlets.md). For why AORC is the primary forcing and
how other hydrological studies chose theirs, see
[forcing used in hydrological practice](model_data_sources.md).

## One-command workflow

After the one-time installation and CDS token setup below, use:

```bash
python research/download_daily.py --geojson outputs/projects43_independent/dam_catchments.geojson --start 2025-12-29 --end 2025-12-31 --output outputs/catchment_daily.csv
```

This single-line command works in Bash, Git Bash and PowerShell. It reads all
local projects in the GeoJSON and performs three stages automatically:

1. AORC, ERA5-Land and ERA5 download and daily catchment statistics, all three
   at the same time and, by default, from the latest data backwards.
2. Verified values and QC exports, each with one row per project and UTC day.
3. Consistency, plausibility and source-agreement checks with plots, then the
   requested-variable table and its README.

The same `--output` argument produces the delivery:

- `outputs/catchment_daily.csv`: the 25 requested variables, 31 columns
  (`date`, `project_id`, 28 values: one source per variable, soil
  temperature in its four layers, and `aorc_gap_filled`).
- `outputs/catchment_daily_README.md`: for every requested column, its source,
  whether it is a native source field or computed here, units, daily
  definition, matching ECMWF IFS / NOAA GFS / WeatherNext 2 forecast fields,
  and the QC results of this delivery.
- `outputs/catchment_daily_full.csv`: every variable, 64 columns (`date`,
  `project_id` and 62 source-qualified values, including the ERA5-Land
  counterparts of the AORC forcing used for cross-checks).
- `outputs/catchment_daily_full_qc.csv`: 250 columns (the same two keys and
  four quality fields per full-table variable).
- `outputs/catchment_daily_full.csv.manifest.json`: units, column definitions
  and provenance for the full tables.

The requested table copies its columns from the full table; its names drop the
source prefix (the README gives the source). Where an AORC value is blank
because the AORC archive lacks the data (for example all of 2024-06-18), the
requested table fills it from the same ERA5-Land quantity adjusted to AORC for
that catchment and calendar month and names the filled columns in
`aorc_gap_filled`; the full table is never filled. See the
[daily data notes](data_notes.md). The
three-day, 43-project example has 129 rows in each table, in the same order.
Missing values retain their quality flags in the QC table; completion does not
imply that every observation exists. No separate export command is needed.

The `outputs/catchment_daily_full.csv.checks/` directory contains
`coverage.png`, `source_comparison.png` (every quantity available from both
AORC and ERA5-Land), `timeseries.pdf` (one page per project), numerical checks
and per-variable QC summaries. See the [daily data notes](data_notes.md) for
measurement heights, signs, definitions and interpretation. To check an
existing delivery and rebuild the requested table and README without
downloading, run:

```bash
python research/check_daily.py outputs/catchment_daily_full.csv
python research/deliver_daily.py outputs/catchment_daily_full.csv
```

The required arguments are `--geojson`, `--start` and `--end`. If `--output`
is omitted, the CSV is named `outputs/catchment_daily_START_END.csv`. For the
full period, use a new output filename:

```bash
python research/download_daily.py --geojson outputs/projects43_independent/dam_catchments.geojson --start 1996-01-01 --end 2025-12-31 --output outputs/catchment_daily_1996_2025.csv
```

The script requires `zstd` and checks the local CDS configuration before
starting AORC. This configuration check does not prove that the provider will
accept the account, terms or requests. The three sources run concurrently: AORC
is limited by S3 reads and decompression, the CDS products by the provider's
queue. AORC processes the latest year first and the CDS products queue and
process the latest months first, so recent data are usable early; pass
`--oldest-first` to reverse this. If one source fails, the others continue and
the final delivery is not replaced. Repeating the same command reuses
hash-verified completed years/months and available source cache; an
interrupted AORC year is recomputed.

### Long runs on a compute host

The CDS queue sets the duration of a 30-year run. At October 2026 rates an AORC
year takes about 20 minutes of processing (roughly 10 hours for 30 years,
overlapping with its S3 download). ERA5 is read by default from Google's public
ARCO-ERA5 copy, which has no queue: about 35 seconds and 3.7 GB per month,
roughly 4 hours and 1.3 TB for 30 years (`--era5-source cds` uses the CDS
instead; both give the same values to GRIB packing precision). ERA5-Land needs
about 1,100 CDS requests: its
17 hourly fields cost more than twice the per-request limit for a month, so
each month takes three requests. In October 2026 a request waited 5-35 minutes
in the queue, so with three in flight ERA5-Land takes several days. The CDS
rejects further submissions once a user has a few requests queued for a
dataset (five was too many), so raising `--cds-workers` above 3 mostly adds
rejected submissions. `--land-source edh` reads ERA5-Land instead from the
[DestinE Earth Data Hub](https://earthdatahub.destine.eu/) copy, which has no
queue; for the 43 catchments this is about 1,200 chunk reads per year, within
the free allowance of 500,000 a month. Before a long run, compare one day of
the store with the CDS over the catchments' area with
`python research/check_edh.py --compare-cds --geojson <catchment file>`. Every source
works through the period year by year, latest first, and writes each finished
year (AORC) or month (CDS) as it goes.

Before starting, on the host that will run the download:

1. Clone or update the repository, create a Python 3.10+ environment and run
   `python -m pip install -e . -r research/requirements.txt`; check
   `zstd --version`.
2. Put `~/.cdsapirc` there (section 2) and accept the ERA5-Land licence once
   on the CDS website (the ERA5 single-level licence too if `--era5-source cds`).
   For `--land-source edh`, also add the DestinE personal access token to
   `~/.netrc` as `machine data.earthdatahub.destine.eu password <token>` and
   run `chmod 600 ~/.netrc`.
3. Copy the catchment file to `outputs/projects43_independent/dam_catchments.geojson`
   (the `outputs/` folder is not in Git), or rebuild it with `hydro-map build`
   (section 3). Every run of a project must use the same file.
4. Use a node that can reach the internet (AWS S3, Google Cloud Storage and the
   CDS). Many clusters
   block it on compute nodes; use a login, data-transfer or interactive node
   that allows long-running processes, following the site's rules.

Start the run in a terminal multiplexer so it survives logging out:

```bash
tmux new -s hydro
python research/download_daily.py --geojson outputs/projects43_independent/dam_catchments.geojson --start 1996-01-01 --end 2025-12-31 --output outputs/catchment_daily_1996_2025.csv --work-dir /scratch/hydro/work_1996_2025 2>&1 | tee -a download_1996_2025.log
```

Detach with `Ctrl-b d` and return with `tmux attach -t hydro`. Without tmux,
start the same command with `nohup ... > download_1996_2025.log 2>&1 &`. On a
batch scheduler, put the command in the job script; when the wall time ends,
submit the same job again and it continues where it stopped.

Check progress at any time, from any terminal:

```bash
python research/progress_daily.py /scratch/hydro/work_1996_2025
tail -n 20 download_1996_2025.log
```

`progress_daily.py` prints the AORC years processed, and for ERA5-Land and ERA5
the CDS requests downloaded and months processed, with the time of the last
change. In the log, `synchronized native fields chunk N/61` is AORC progress
within a year, `Cached reanalysis-...` is a finished CDS request, `saved ...
daily values` a processed month, and `Retrying ... temporarily limited` is the
CDS queue limit, which is normal. If no file has changed for several hours,
look at the end of the log. To stop, press `Ctrl-c` in the tmux window (or
`kill` the process); running the identical command again resumes from the
completed years, months and downloaded requests. Do not run two commands on the
same working directory at once.

Put `--work-dir` on large local or scratch storage. Raw AORC chunks are
discarded after use unless `--keep-chunks` is given (about 30 GB per year,
useful to reprocess without downloading); CDS responses are always kept (about
5 GB per year of ERA5-Land). With `--land-source edh`, the raw Earth Data Hub
chunks of the current year are kept in `cache/cds/edh_chunks` and deleted once
its requests are answered. While the sources download, completed years and
months can already be plotted (see [Plots](#plots)). When all three finish, the
command exports, checks and writes the delivery files listed above.

Post-export checks run on the saved delivery. If they fail, the files remain
available, the command returns status 2, and the log gives the check-only command.
Missing values are reported separately and are not filled automatically.

Working files default to `OUTPUT.work/`, with `aorc/`, `era5-land/` and `era5/`
source directories and a `cache/` directory. Retain this folder to resume.
For a compute host, `--work-dir` and `--cache-dir` can point to large local
storage while `--output` points to the delivery location. Existing files from
separate manual runs are not automatically discovered or moved. Do not run
two commands against the same working/cache directories concurrently. After
changing dates, geometry or processing code, use a new working directory;
the source pipelines reject incompatible run configurations. One exception
allows a CDS processing update when no monthly output or completion manifest
exists yet and the request configuration is unchanged. The previous run
metadata is retained under `run_history/`, and verified raw responses are
reused. This does not change AORC resume checks.

CDS coordinate encodings can differ slightly across files. Regular axes are
reconstructed between their endpoints with an absolute residual limit of
0.00002 degrees, covering float32 coordinate rounding. Cross-field checks
require equal dimensions and coordinate differences within the same absolute
limit; values are not interpolated. A larger shift, different resolution or
different dimensions still stops processing. A cross-field mismatch writes
`grid_mismatch.json` in the affected source directory with the field names,
axis lengths, bounds, spacing and maximum differences. After a precision-check
fix, repeat the original command with the same paths and options to reuse the
downloaded data. Keep the working folder and cache intact.

`--max-download-gb` defaults to a 2,000 GB **AORC network-read ceiling per
invocation**, not a file-size estimate or a CDS limit. Use
`--max-download-gb 2` to bound a small demo. The other optional controls are
`--workers` (concurrent AORC reads and decompressions, default 16, at most 32)
and `--chunk-days` (ERA5-Land state batches, default 14).
The CDS rejects an ERA5-Land request whose size cost (variables × hourly
steps × 2) exceeds 12,000; with the 17 hourly ERA5-Land fields this allows at
most 14 days, and the downloader refuses a larger value before contacting the
CDS. Each CDS product keeps three requests queued at once (`--cds-workers`).
A queue-limit rejection ("Number queued requests for this dataset is
temporarily limited") is retried every two minutes for up to 12 hours; server
errors and network failures up to 30 times. Invalid or oversized requests and
missing licences are not retried. A request that fails does not stop the
others: the run reports it after the rest are cached, and rerunning the same
command fetches only what is missing.
For Parquet, install the optional dependency below and use a `.parquet` output.

Routing interpretation, including Kootenay Canal's tailrace/diversion note,
is retained in source `run.json` files and the final manifest. These static
notes are no longer repeated in the console. Missing-data messages and source
errors remain visible. This changes reporting only, not the catchment or
meteorological calculations.

The source-specific commands later in this guide are available for individual
source checks; they are not additional steps after `download_daily.py`.

## 1. Install

Run commands from the repository root in an activated Python 3.10+ environment.
The examples use Bash, including Git Bash on Windows; PowerShell users can put
each command on one line instead of using Bash line continuations.

```bash
python -m pip install -e . -r research/requirements.txt
```

AORC also needs the `zstd` command-line decoder on `PATH`. For example, in a
Conda environment install it with `conda install -c conda-forge zstd`. Verify
with `zstd --version`. The CDS path uses the official `cdsapi` client and
NetCDF4. To enable optional Parquet output:

```bash
python -m pip install -r research/requirements-parquet.txt
```

## 2. Configure the CDS token

ERA5 and ERA5-Land use a **Climate Data Store Personal Access Token**. Register
or sign in at the [CDS API setup page](https://cds.climate.copernicus.eu/how-to-api)
and copy the configuration shown for your account into `.cdsapirc` in the home
directory of the account that will run the downloads:

| Runtime | File location |
| --- | --- |
| Linux or macOS | `~/.cdsapirc` |
| Windows | `C:\Users\<username>\.cdsapirc` |
| Remote compute host | `~/.cdsapirc` on that host, under the job's user account |

The file contains:

```yaml
url: https://cds.climate.copernicus.eu/api
key: <YOUR_PERSONAL_ACCESS_TOKEN>
```

On Unix, set `chmod 600 ~/.cdsapirc`. On Windows, ensure the filename is exactly
`.cdsapirc`, not `.cdsapirc.txt`. To find the home directory used by Python:

```bash
python -c "from pathlib import Path; print(Path.home() / '.cdsapirc')"
```

Keep the token outside the repository. The scripts use `cdsapi.Client()` and do
not accept or save a token as a command-line argument. A local configuration
does not automatically configure a remote host. Before the first real request,
accept the terms for each required dataset on its CDS download page:

- [ERA5-Land hourly](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land?tab=download)
- [ERA5 single levels](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels?tab=download)

The request preview described below does not need a token. It checks local
configuration and constructs requests; it does not verify account permissions,
accepted terms, provider availability or live response formats.

## 3. Build the boundaries

```bash
hydro-map build configs/columbia.yaml --cache-dir data \
  --output outputs/projects43_independent/dam_catchments.geojson \
  --table-output outputs/projects43_independent/dam_catchments.csv \
  --csv-output outputs/projects43_independent/polygon_grid_bbox.csv
```

Use this exact GeoJSON for every source. The meteorological workflow uses its
full polygon, not the bounding-box CSV, a dam point or a centroid. Keep the
same catchment part and upstream cutoffs across sources. Changes to the
GeoJSON require new output directories and new spatial weights. Downloaded
data and generated geometry are not included in Git.

## 4. Run a short test first

Start with all 43 projects for December 29-31, 2025. This tests the calendar
boundary as well as the complete project set. AORC downloads can be large even
for a short time window because its native objects span several days.

```bash
python research/download_aorc.py \
  --geojson outputs/projects43_independent/dam_catchments.geojson \
  --output-dir outputs/aorc_demo_derived --cache-dir data/aorc_cache \
  --start 2025-12-29 --end 2025-12-31 --derive \
  --workers 4 --max-download-gb 2 --keep-chunks

python research/download_cds.py --product era5-land \
  --geojson outputs/projects43_independent/dam_catchments.geojson \
  --output-dir outputs/era5_land_demo_cds --cache-dir data/cds_cache \
  --start 2025-12-29 --end 2025-12-31 --dry-run

python research/download_cds.py --product era5 \
  --geojson outputs/projects43_independent/dam_catchments.geojson \
  --output-dir outputs/era5_demo_cds --cache-dir data/cds_cache \
  --start 2025-12-29 --end 2025-12-31 --dry-run
```

After reviewing the CDS request previews and configuring credentials, repeat
the two CDS commands without `--dry-run`. They retrieve data and process daily
catchment statistics. AORC requires no account. Each CDS product retains its
own source identity; ERA5-Land is not silently substituted for AORC.

## 5. Download the full period

Choose output and cache directories on a compute host with enough disk space
and reliable networking. The following paths are examples, relative to the
repository; replace them with the storage paths on the host. The AORC transfer
ceiling is a hard upper bound, not an estimate or a prepaid allocation.

```bash
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python research/download_aorc.py \
  --geojson outputs/projects43_independent/dam_catchments.geojson \
  --output-dir data/aorc_1996_2025_derived --cache-dir data/aorc_cache \
  --start 1996-01-01 --end 2025-12-31 --derive \
  --workers 4 --max-download-gb 2000

python research/download_cds.py --product era5-land \
  --geojson outputs/projects43_independent/dam_catchments.geojson \
  --output-dir data/era5_land_1996_2025 --cache-dir data/cds_cache \
  --start 1996-01-01 --end 2025-12-31

python research/download_cds.py --product era5 \
  --geojson outputs/projects43_independent/dam_catchments.geojson \
  --output-dir data/era5_1996_2025 --cache-dir data/cds_cache \
  --start 1996-01-01 --end 2025-12-31
```

The two CDS products may run at the same time with a shared cache: their
request manifest is updated under a file lock. Most of the time is spent in the
CDS queue (typically 5-15 minutes per request in October 2026), so requests
are as large as the limits allow: hourly ERA5-Land states in batches of at most
14 days within a month (the size limit above), ERA5-Land 24-hour endpoints in
one request per year, and ERA5 single-level fields in one request per month.
For 2024-2025 this is 75 ERA5-Land and 24 ERA5 requests. All uncached requests
are queued first, three at a time per product by default (`--workers`); daily
processing then decodes each hash-verified cached response once per batch.

Repeat an identical command to resume verified completed periods. AORC writes
annual files; CDS writes monthly files. Configuration and output hashes are
checked before reuse. Do not overwrite an old run after changing processing
code, geometry or options; use a new directory, except for the unfinished CDS
processing update described above. Keep the source manifests with
the data. Raw AORC meteorological chunks are discarded after all requested
base and derived products consume them unless `--keep-chunks` is supplied.

## 6. Export one project-day row

After the required source periods have completed:

```bash
python research/export_daily.py \
  --geojson outputs/projects43_independent/dam_catchments.geojson \
  --input-dir data/aorc_1996_2025_derived data/era5_land_1996_2025 data/era5_1996_2025 \
  --start 1996-01-01 --end 2025-12-31 \
  --output outputs/delivery/catchment_daily_1996_2025_full.csv
python research/check_daily.py outputs/delivery/catchment_daily_1996_2025_full.csv
python research/deliver_daily.py outputs/delivery/catchment_daily_1996_2025_full.csv
```

`deliver_daily.py` writes `catchment_daily_1996_2025.csv` (requested columns)
and `catchment_daily_1996_2025_README.md` beside the full table.

Use `.csv.gz` for compressed CSV or `.parquet` for optional Parquet output.
The QC table uses the same format: `example.csv.gz` produces
`example_qc.csv.gz`, and `example.parquet` produces `example_qc.parquet`.
The same command can export a short test period from the three demo directories
or re-export existing completed source periods without downloading again. For
the one-command example above, use these source directories:

```bash
python research/export_daily.py --geojson outputs/projects43_independent/dam_catchments.geojson --input-dir outputs/catchment_daily.csv.work/aorc outputs/catchment_daily.csv.work/era5-land outputs/catchment_daily.csv.work/era5 --start 2025-12-29 --end 2025-12-31 --output outputs/catchment_daily_full.csv
```

Let an existing download finish before updating its checkout or re-exporting
its source files. This delivery-format change does not change the downloader
configuration or require source data to be downloaded again.

The exporter requires completed, hash-verified source periods. It rejects
duplicate observations, conflicting units, different GeoJSON hashes and
mismatched project sets. Missing source periods are an error; a missing value
inside a completed period remains a flagged blank.

Both tables share the unique key `(date, project_id)` and the same row order.
Source-qualified columns prevent
overwrites, for example `aorc_v1_1__precipitation_mm` and
`era5_land_cds__precipitation_mm`. Each value's QC fields appear in the QC
table with the suffixes `__qc`, `__valid_hours`, `__expected_hours` and
`__min_valid_area_fraction`. For example, `aorc_v1_1__tmean_degC__qc`
describes `aorc_v1_1__tmean_degC` in the values table.

The companion `<output filename>.manifest.json` uses `schema_version: 2`.
Its `columns` and `qc_columns` dictionaries describe the two tables;
`output_sha256`, `qc_output_sha256` and `qc_file` identify the paired outputs.
It also retains original source-variable names, units, aggregation definitions,
input hashes and project metadata. Deliver both tables and the manifest.
The destination is supplied through `--output`.

## Plots

`research/plot_daily.py` draws one catchment from a delivery
(`OUTPUT_full.csv`) or from a working directory while it is still downloading;
it uses whatever years and months are complete. Gaps in the data break the
lines instead of being bridged. Long periods are shown as weekly or monthly
points (`--resample` chooses daily `D`, weekly `W` or monthly `M`; amounts are
summed over complete periods, states averaged). Figures are written to
`figures/` unless `--output` names a PNG, PDF or SVG file.

```bash
python research/plot_daily.py outputs/catchment_daily_1996_2025.csv.work --project MICA
python research/plot_daily.py outputs/catchment_daily_1996_2025.csv.work --project MICA --kind wateryear --variable snow_water_equivalent_mm
python research/plot_daily.py outputs/catchment_daily_1996_2025_full.csv --project BROWNLEE --kind compare --variable precipitation_mm --start 2015-10-01
```

| Kind | What it shows | What to look for |
| --- | --- | --- |
| `overview` (default) | Stacked panels on one time axis: precipitation split into rain and snow, daily mean air temperature with the daily minimum-maximum range and the 0 °C line, snow water equivalent, surface and root-zone soil moisture, actual and potential evapotranspiration, and the 0 °C level above sea level | Snow accumulates while temperature stays below 0 °C and melts as it rises; soil moisture should rise with melt and rain and fall while evapotranspiration is high; rain-on-snow appears as rain bars over a snowpack |
| `compare` | One variable from every source that has it (AORC, ERA5-Land, ERA5) and, below, ERA5-Land minus AORC with its mean and correlation | A steady offset is a bias that can be corrected; a drifting or seasonal difference, or low correlation, means the sources disagree on timing |
| `wateryear` | One variable for each water year (1 October to 30 September): running totals for daily amounts (precipitation, snowfall, snowmelt, evapotranspiration), daily values for states (snow water equivalent, soil moisture); earlier years in grey, their median dashed and the latest year highlighted | Whether the current year is wetter or drier, and its snowpack larger or smaller, than usual at the same date; the timing of peak snow water equivalent and melt-out |

`--start` and `--end` limit the period; `--source` selects the source for
`wateryear`. Variable names are the column names without source prefix and
with the units suffix as stored in the source files, for example
`precipitation_mm`, `tmean_c`, `snow_water_equivalent_mm` or
`root_zone_soil_moisture_0_100cm_m3m3`.

## Variables and calculations

The names below are exported value-column names without their source prefixes.
Unit suffixes are explicit, such as `degC`, `m_s`, `kg_kg`, `Pa`, `kPa`,
`W_m2`, `MJ_m2`, `kg_m3` and `m3_m3`. Source daily files retain their
original variable identifiers; the manifest maps them to the exported names.

### AORC: weather inputs and derived diagnostics

The [NOAA AORC v1.1 archive](https://registry.opendata.aws/noaa-nws-aorc/)
provides eight hourly fields on a 30 arc-second grid: precipitation,
temperature, specific humidity, surface pressure, downward shortwave and
longwave radiation, and 10 m U/V wind. The published
[NWRFC calibration framework](https://repository.library.noaa.gov/view/noaa/73306/noaa_73306_DS1.pdf)
uses precipitation, temperature, humidity and pressure for its meteorological
inputs and rain/snow partitioning. Using this data source does not reproduce
NWRFC calibration, routing or reservoir operations.

| Requested quantity | Output or calculation | Daily units and meaning |
| --- | --- | --- |
| Total precipitation | `precipitation_mm` | mm accumulated over the UTC day |
| 2 m air temperature | `tmean_degC` | Mean of 24 hourly catchment means, °C |
| Daily minimum/maximum temperature | `tmin_degC`, `tmax_degC` | Extrema of hourly catchment-mean temperature, °C; not averages of cellwise extrema |
| Specific humidity | `specific_humidity_kg_kg` | Daily mean, kg/kg |
| Surface pressure | `surface_pressure_Pa` | Daily mean, Pa |
| Shortwave radiation | `shortwave_down_mean_W_m2`, `shortwave_down_energy_MJ_m2` | Incoming flux, W/m²; hourly integration estimate, MJ/m²/day |
| Longwave radiation | `longwave_down_mean_W_m2`, `longwave_down_energy_MJ_m2` | Incoming flux, W/m²; hourly integration estimate, MJ/m²/day |
| U/V wind | `u_wind_m_s`, `v_wind_m_s` | Mean eastward/northward 10 m components, m/s |
| Wind speed | `wind_speed_m_s` | Mean of native hourly `sqrt(u²+v²)`, m/s |
| Relative humidity | `relative_humidity_pct` | Native T/q/pressure calculation followed by averaging, % |
| Vapor pressure deficit | `vapor_pressure_deficit_kPa` | Native T/q/pressure calculation followed by averaging, kPa |
| Wet-bulb temperature | `wet_bulb_temperature_degC` | Pressure-aware psychrometric calculation, °C |
| Rainfall and snowfall | `rainfall_mm`, `snowfall_mm` | Water-equivalent phase amounts, mm/day; sum to total precipitation when all inputs are valid |
| Potential evapotranspiration | `pet_hargreaves_mm_day` | Hargreaves-Samani estimate, mm/day |

`--derive` enables the additional diagnostics and PET. Without it, the
downloader retains the original eight-input workflow. Existing hourly
catchment means cannot recover exact native-grid RH, speed or precipitation
phase. To add these products, read the native inputs again with `--derive`.

The humidity calculation uses `e = q p / (0.622 + 0.378 q)` and the documented
saturation-vapor-pressure formula over liquid water. RH is `100 e/es(T)`;
supersaturated values are retained. VPD is `max(es(T) - e, 0)`. Wet-bulb
temperature solves a pressure-aware psychrometric equation. Rain/snow is a
binary diagnostic: snow at wet-bulb temperatures at or below 0.5°C, rain above
0.5°C. It is not a direct snowfall observation. The wet-bulb solve uses
liquid-water saturation even below freezing and caps vapor pressure at
saturation for this calculation only; supersaturated inputs give wet bulb
equal to air temperature. Conditions at the precipitation timestamp classify the hourly
amount ending at that timestamp. Each calculation uses the intersection of
valid input masks before spatial aggregation.

PET uses the daily catchment-mean temperature statistics, polygon-centroid
latitude, date and extraterrestrial radiation. The Hargreaves-Samani formula
is `0.0023 (Tmean + 17.8) sqrt(Tmax - Tmin) Ra`, with radiation converted from
MJ/m²/day to equivalent mm/day using 0.408. Negative PET is set to zero. This
is an explicitly selected estimation method, not ERA5-Land potential
evaporation and not a calibrated crop-specific water demand.

### ERA5-Land: snow, soil and surface fluxes

The complete archive comes from the
[CDS hourly product](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land).
It provides model estimates on a 0.1° distribution grid (native resolution
about 9 km). With `--land-source edh` the same hourly fields are read from the
Earth Data Hub ERA5-Land store,
a Zarr copy in chunks of 60 days by 5° × 10°, stored with 13 mantissa bits
(relative precision about 1e-4). `research/edh_era5_land.py` answers the CDS
requests from it and writes the same NetCDF layout, so caching and daily
processing are unchanged; the store's units and accumulation type are checked
like a CDS response.

| Requested quantity | Output or calculation | Daily units and meaning |
| --- | --- | --- |
| Snow water equivalent | `snow_water_equivalent_mm` from `sd` | Daily mean water equivalent, mm |
| Physical snow depth | `snow_depth_m` from the separately identified `sde` field | Daily mean physical depth, m; never relabel `sd` as physical depth |
| Snow density and cover | `snow_density_kg_m3`, `snow_cover_pct` | Supporting snow diagnostics, kg/m³ and % |
| Snowmelt | `snowmelt_mm` | Modeled water-equivalent melt, mm/day |
| Surface soil moisture | `soil_moisture_layer1_m3_m3` | Volumetric water content, 0-7 cm, m³/m³ |
| Root-zone soil moisture | `root_zone_soil_moisture_0_100cm_m3_m3` | Defined here as the 0-100 cm thickness-weighted mean |
| Deep soil moisture | `soil_moisture_layer4_m3_m3` | Volumetric water content, 100-289 cm, m³/m³ |
| Soil temperature | `soil_temperature_layer1_degC` through `soil_temperature_layer4_degC` | Separate daily means for the four layers, °C |
| Actual evapotranspiration | `actual_evapotranspiration_mm` from total evaporation | mm/day, positive upward water loss; negative values represent net deposition/condensation |
| Net shortwave/longwave radiation | `net_shortwave_energy_MJ_m2`, `net_longwave_energy_MJ_m2` | Accumulated net energy, MJ/m²/day |
| Total net radiation | `net_radiation_energy_MJ_m2`, `net_radiation_mean_W_m2` | Net solar plus net thermal energy, MJ/m²/day, and equivalent mean flux, W/m² |
| Downward radiation | `shortwave_down_mean_W_m2`, `longwave_down_mean_W_m2` and `_energy_MJ_m2` from `ssrd`, `strd` | Daily mean flux and energy of the 24-hour accumulation |
| Net shortwave/longwave flux | `net_shortwave_mean_W_m2`, `net_longwave_mean_W_m2` | The same accumulations as equivalent mean flux, W/m² |
| Forcing counterparts of AORC | `tmean_degC`, `tmin_degC`, `tmax_degC`, `precipitation_mm`, `rainfall_mm`, `snowfall_mm`, `specific_humidity_kg_kg`, `relative_humidity_pct`, `vapor_pressure_deficit_kPa`, `wet_bulb_temperature_degC`, `surface_pressure_Pa`, `u_wind_m_s`, `v_wind_m_s`, `wind_speed_m_s` | Same names and daily definitions as AORC for direct comparison. Humidity is calculated from 2 m dewpoint and surface pressure with the AORC formulas; snowfall is the native ERA5-Land `sf` and rainfall is `tp - sf` |

All four soil-water layers are retained. Layer boundaries are 0-7, 7-28,
28-100 and 100-289 cm. The defined root-zone mean is
`0.07*layer1 + 0.21*layer2 + 0.72*layer3`; other rooting depths require a
different definition.

ERA5-Land accumulated fields use the following midnight's forecast endpoint
for the preceding UTC day. They are not summed across cumulative hourly
steps. For those fields, one complete endpoint represents 24 hours:
`valid_hours=24` is the represented accumulation duration, not a claim that
24 independent hourly observations were retrieved. State means require all
24 hourly values. See the
[ECMWF accumulation and variable documentation](https://confluence.ecmwf.int/pages/viewpage.action?pageId=505384848).

### ERA5: clouds and the freezing level

The [single-level product](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels)
supplies total cloud cover, the 0°C level and surface geopotential. No
pressure-level data are needed. By default these fields are read from
[ARCO-ERA5](https://cloud.google.com/storage/docs/public-datasets/era5), Google's
public copy of the same 0.25° hourly data, which answers each monthly request in
under a minute instead of waiting in the CDS queue. For 2024 test months the
ARCO and CDS responses had identical grids and times and differed by at most
8e-6 in cloud fraction, 0.05 m in 0°C level and 0.45 m²/s² in geopotential,
the precision of GRIB packing.

`cloud_cover_fraction` is a daily mean fraction, 0-1.
`freezing_level_above_ground_m` is the native ECMWF `zero_degree_level`
(`deg0l`): the height above the surface where temperature passes from positive
to negative, computed by ECMWF on the model's 137 vertical levels. It is zero
when the whole column is below 0°C; with more than one warm layer, ECMWF assigns
the top of the second atmospheric layer ([ECMWF parameter 228024](https://codes.ecmwf.int/grib/param-db/228024)).
`freezing_level_above_sea_level_m` adds the ERA5 surface geopotential height
(`z/9.80665`) in every cell and hour, as ECMWF recommends; it equals the model
terrain height when the column is below freezing. This is the requested
"0°C isotherm elevation". Both are defined at every cell and hour, so daily
means are never blank because of a cold or inverted profile.

The values table contains **62 data variables**: 19 from AORC with
`--derive`, 40 from ERA5-Land and 3 from ERA5. Together with `date` and
`project_id`, the full table has 64 columns. Each variable has four fields in
the separate QC table, which has 250 columns including the same two keys.
The requested table holds the 25 requested variables in 28 columns (13 AORC,
12 ERA5-Land and 3 ERA5); `research/delivery_variables.py` lists them with their forecast
counterparts. Daily accumulated quantities use `mm` or `MJ/m2` in the
machine-readable unit dictionary; the daily interval makes them numerically
equivalent to the mm/day and MJ/m²/day amounts in the tables above.

## Code reference

| File | Purpose |
| --- | --- |
| `research/download_daily.py` | One command for all three sources, daily processing, resume, checks and the final delivery |
| `research/download_aorc.py` | Retrieve native AORC fields and calculate polygon series; `--derive` enables diagnostics |
| `research/meteorology.py` | Humidity, wet bulb, rain/snow partition, wind speed and Hargreaves PET formulas |
| `research/aggregate_daily.py` | Aggregate hourly polygon series with explicit UTC timing and coverage rules |
| `research/download_cds.py` | Retrieve ERA5-Land/ERA5, calculate polygon statistics and write monthly daily files |
| `research/cds_fields.py` | CDS variable definitions, soil weighting, humidity, wind and freezing-level conversions |
| `research/arco_era5.py` | Serves ERA5 single-level requests from the public ARCO-ERA5 copy in the CDS response layout |
| `research/edh_era5_land.py` | Serves ERA5-Land requests from the Earth Data Hub copy in the CDS response layout |
| `research/check_edh.py` | Checks the Earth Data Hub store: units, accumulation convention and, with `--compare-cds`, values against the CDS |
| `research/export_daily.py` | Verify source artifacts and export aligned daily values and QC tables plus their manifest |
| `research/check_daily.py` | Check delivered values/QC, physical plausibility and AORC/ERA5-Land agreement; plot coverage and project time series |
| `research/deliver_daily.py` | Write the requested-variable table and README from a checked full delivery |
| `research/delivery_variables.py` | Requested columns: source, native or computed, definition and forecast counterparts |
| `research/plot_daily.py` | Overview, source comparison and water-year plots for one catchment, from a delivery or a working directory |
| `research/progress_daily.py` | Progress of a running or interrupted download: years, requests and months completed |

For modeling, begin with AORC precipitation and temperature as meteorological
forcing. Rain/snow partition and PET are calculated inputs with the method
defined here. Use the ERA5-Land states and fluxes only where your model needs
external states, predictors or evaluation data; do not count externally
supplied snowmelt/ET a second time if the hydrological model already computes
those processes. Preserve source prefixes and keep the QC table linked by
`(date, project_id)` when preparing model inputs. The split preserves all
variables and quality checks; it does not choose features or impute values.
Missing or flagged values require a separately chosen treatment.

## Quality, interpretation and validation

Validation on 2026-10-06 passed 76 core tests and 120 research tests, including
all three source-processing contracts through the final exporter across a
December/January boundary using synthetic CDS responses. The unified entry
point also tests resume without repeat retrieval, routing-note retention,
early credential failure and preservation of a previous delivery after a
source failure. CDS tests cover coordinate rounding, rejection of real grid
shifts and dimension changes, and resuming a failed grid check after a code
update without downloading AORC or the cached ERA5-Land responses again.
A real cached AORC
replay for 43 projects over 2025-12-29 through 2025-12-31 produced 2,451 daily
records: 2,322 valid and 129 flagged precipitation/rain/snow values on the
last date. Rain plus snow matched precipitation in all 86 complete project-days
within 1.1e-13 mm. CSV and Parquet exports both contained 129 rows. Re-exporting
this AORC sample into the split format preserved all 2,451 numeric/missing
values and their QC records exactly. The values table had 21 columns and the
QC table 78 columns for these 19 AORC variables. Three-source integration
tests verify 47 value columns and 182 QC columns, including typed Parquet
nulls and rollback if either output or its manifest cannot be published. The CDS
request previews cover all 43 projects. Local CDS tests use synthetic
responses; authenticated end-to-end CDS output has not been validated here.

- Area averages include fractional native grid-cell intersections. Invalid
  cells never become zero and valid cells are not renormalized to represent
  missing areas. Each source records its weighting method.
- A UTC precipitation day uses amounts ending at 01Z through the next 00Z.
  State statistics use 00-23Z. Align observed flow days before fitting a model.
- The inspected AORC archive lacks 2026-01-01 00Z, needed for the last hour of
  2025-12-31 precipitation. Its final precipitation total and dependent phase
  totals remain blank. Do not replace them with a 23-hour sum or silently
  splice an ERA5-Land hour into AORC.
- AORC is a gridded analysis assembled from observations and other products;
  ERA5-Land snow/soil/ET are model estimates. They are not basin-wide sensor
  measurements or demonstrated hydrological truth.
- The source-labelled snow, soil and ET products do not automatically form a
  closed water balance with AORC precipitation. Evaluate them as additional
  predictors, initialization information or diagnostics. Training and
  validation splits must prevent future information entering model features.
- Codes, units, geometry hashes and source identities are retained. Successful
  unit tests or request previews do not establish 30-year source completeness
  or hydrologic forecast skill. Live CDS verification requires a configured
  account and accepted dataset terms.

Run the repository checks with:

```bash
python -m unittest discover -s tests
python -m unittest discover -s research -p 'test_*.py'
```
