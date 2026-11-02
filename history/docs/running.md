# Setting up and running

How to set up a host, build the historical table once and keep it up to date
every day. Details of each step are in the [pipeline guide](pipeline.md); how
to read the table is in the [data notes](data_notes.md).

## 1. Set up a host (once)

The host needs Linux, Python 3.10 or newer, and outbound internet to AWS S3
(AORC), Google Cloud Storage (ARCO-ERA5), the Climate Data Store and the
Destination Earth Data Hub. Compute nodes of many clusters have no internet;
use a login, data-transfer or interactive node, or a workstation.

1. Get the code and install it:

   ```bash
   git clone <repository> && cd <repository>/history
   python -m venv .venv && source .venv/bin/activate
   python -m pip install -e '.[parquet]'
   ```

   In a shared environment that you cannot install into, set
   `export PYTHONPATH=/path/to/history/src` and write
   `python -m catchment_history` wherever this guide writes `catchment-history`.

2. Install the `zstd` decoder for AORC (`conda install -c conda-forge zstd`)
   and check `zstd --version`.

3. Climate Data Store: create an account, copy the key from the
   [API setup page](https://cds.climate.copernicus.eu/how-to-api) into
   `~/.cdsapirc` and accept the licences of
   [ERA5-Land hourly](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land?tab=download)
   and [ERA5 single levels](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels?tab=download):

   ```text
   url: https://cds.climate.copernicus.eu/api
   key: <YOUR_PERSONAL_ACCESS_TOKEN>
   ```

4. Earth Data Hub: create a free DestinE account and put its token in
   `~/.netrc` as one line:

   ```text
   machine data.earthdatahub.destine.eu password <YOUR_EDH_TOKEN>
   ```

5. Run `chmod 600 ~/.cdsapirc ~/.netrc`. Keep both files out of the
   repository; the commands read them and never take a key as an argument.

6. Copy the catchment GeoJSON files to the host (for example the 43 Columbia
   catchments and the 3 Peace River catchments). Every run of a catchment set
   must use the same file; see [catchment file](pipeline.md#3-catchment-file).

7. Check: `catchment-history --help` lists the commands.

## 2. Build the historical table (once)

Put working folders on large scratch storage (about 30 GB of AORC chunks per
year pass through it; ERA5-Land responses take about 5 GB per year and are
kept). Every command resumes when repeated with the same arguments. Set `END`
to the last day to include, at most six days before today.

```bash
END=2026-10-24
```

1. AORC catchments, 1996-2025, all three sources (10-12 hours):

   ```bash
   catchment-history download --geojson columbia.geojson --start 1996-01-01 --end 2025-12-31 --output outputs/catchment_daily_1996_2025.csv --work-dir /scratch/hydro/work_1996_2025
   ```

2. Map ERA5-Land onto AORC: fills AORC gaps and replaces AORC in the
   `REPLACED` catchments:

   ```bash
   catchment-history debias --aorc outputs/catchment_daily_1996_2025_full.csv --era5 outputs/catchment_daily_1996_2025_full.csv --output outputs/catchment_daily_1996_2025_mapped.parquet
   ```

3. The days after the AORC archive, from ERA5-Land and ERA5, mapped the same
   way and appended:

   ```bash
   catchment-history download --geojson columbia.geojson --primary era5-land --start 2026-01-01 --end $END --output outputs/era5_2026.csv --work-dir /scratch/hydro/era5_2026
   catchment-history debias --aorc outputs/catchment_daily_1996_2025_full.csv --era5 outputs/era5_2026_full.csv --output outputs/catchment_daily_2026.csv
   catchment-history merge outputs/catchment_daily_1996_2025_mapped.parquet outputs/catchment_daily_2026.csv --output outputs/columbia_daily.parquet
   ```

4. Peace River catchments, north of AORC, from ERA5-Land, over the same dates:

   ```bash
   catchment-history download --geojson peace.geojson --primary era5-land --start 1996-01-01 --end $END --output outputs/peace_daily.parquet --work-dir /scratch/hydro/peace
   ```

5. Join both sets into the delivered table, with its README, manifest and the
   GeoJSON of every catchment beside it:

   ```bash
   catchment-history merge-deliveries outputs/columbia_daily.parquet outputs/peace_daily.parquet --geojson columbia.geojson peace.geojson --output outputs/catchment_daily_46.parquet
   ```

6. Optional checks: `plot-debias` draws AORC beside ERA5-Land before and after
   the mapping and writes `check.csv`, the year-by-year test of the mapping:

   ```bash
   catchment-history plot-debias --aorc outputs/catchment_daily_1996_2025_full.csv --era5 outputs/catchment_daily_1996_2025_full.csv --output-dir figures/debias
   ```

Deliver `catchment_daily_46.parquet` and `catchment_daily_46_README.md`. Keep
`catchment_daily_1996_2025_full.csv` and its manifest unchanged: the daily
update fits its mapping on them.

## 3. Update every day

`scripts/daily_update.sh` downloads the days published since the last run for
every catchment, maps them like the rest of the table and appends them to the
delivered table, which is replaced only when the joined table is complete:

```bash
cd /path/to/history
export DELIVERY=outputs/catchment_daily_46.parquet AORC_FULL=outputs/catchment_daily_1996_2025_full.csv WORK=/scratch/hydro/daily
scripts/daily_update.sh
```

- **When.** Once a day, at any hour. ERA5 and ERA5-Land reach the Climate Data
  Store about five days late, so a run ends six days before today (UTC). A run
  that finds nothing new stops; one that finds a day still blank appends
  nothing, and the next run catches up.
- **How long.** Mostly the Climate Data Store queue for the newest days,
  minutes to a few hours; the mapping and the append take seconds.
- **From cron**, one run at a time:

  ```text
  0 12 * * * cd /path/to/history && . ./daily.env && flock -n /tmp/catchment_daily.lock scripts/daily_update.sh >> daily_update.log 2>&1
  ```

  with the `export` line above (and `PYTHONPATH` and `PYTHON` if needed) in
  `daily.env`.
- **As of an earlier day.** `scripts/daily_update.sh 2026-10-20` stops at that
  day. If the table already runs past it, give a separate output:
  `OUTPUT=outputs/catchment_daily_46_2026-10-15.parquet scripts/daily_update.sh 2026-10-15`.
- **Working folders.** Each run works in `WORK/START_END`; delete them once the
  run has appended. Keep `WORK/cache`, which later runs reuse.

## 4. Things to know

- The newest days are preliminary (ERA5T, ERA5-Land-T). The final data replace
  them on the Climate Data Store two to three months later, usually unchanged;
  the appended days keep the preliminary values until the period after 2025 is
  rebuilt as in section 2.
- The README and manifest beside the delivered table describe its last full
  build; the daily appends do not rewrite them.
- When NOAA releases AORC after 2025, rebuild from section 2 so that the AORC
  catchments take AORC again.
- `Output configuration differs`: a working folder belongs to one set of dates,
  catchments and code. After changing any of them, use a new `--work-dir` and
  pass the old cache with `--cache-dir OLD_WORK_DIR/cache`.
- `CDS queue full ...; retrying in 2 min` is normal; a request is given up
  after 12 hours in the queue.
- A `download` that ends with status 2 has written its tables but some checks
  need attention; the log names them and gives the command that repeats the
  checks without downloading.
- `catchment-history progress WORK_DIR` shows how far a running download has
  got.
