# catchment-history

Daily catchment-mean meteorology from AORC v1.1, ERA5-Land and ERA5. For every
catchment polygon in a GeoJSON file it writes one row per catchment and UTC day:
precipitation, rain and snow, temperature, humidity, radiation, wind, PET,
snow, soil and surface fluxes, each with its source, units and quality fields.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[parquet]'
```

AORC also needs the `zstd` command-line decoder on `PATH`
(`conda install -c conda-forge zstd`).

ERA5 and ERA5-Land need a Climate Data Store key in `~/.cdsapirc`, with the
ERA5-Land and ERA5 single-level licences accepted on the CDS website. ERA5-Land
is read from the DestinE Earth Data Hub, which needs its token in `~/.netrc`:

```text
machine data.earthdatahub.destine.eu password <YOUR_EDH_TOKEN>
```

Keep both files outside the repository and run `chmod 600` on them. See
[credentials](docs/pipeline.md#2-configure-the-cds-and-earth-data-hub-tokens).

## Run

A short sample first:

```bash
catchment-history download --geojson catchments.geojson --start 2025-12-29 --end 2025-12-31 --output outputs/catchment_daily.csv
```

This downloads the three sources side by side, computes the daily catchment
values, checks them and writes:

| File | Content |
| --- | --- |
| `catchment_daily.csv` | The requested variables, one source per variable |
| `catchment_daily_README.md` | Source, definition, units and limitations of every requested column |
| `catchment_daily_full.csv` | Every variable from every source |
| `catchment_daily_full_qc.csv` | Four quality fields per value |
| `catchment_daily_full.csv.manifest.json` | Units, definitions, provenance and hashes |
| `catchment_daily_full.csv.checks/` | Consistency, plausibility and source-agreement reports and plots |

Repeating the same command resumes from the finished years and months. A
30-year run takes about 10-12 hours on a 16-core host; see
[long runs](docs/pipeline.md#long-runs-on-a-compute-host).

The delivered table is written from the full table by `debias`, which fills
AORC gaps, replaces AORC in the catchments listed in `REPLACED`
(`src/catchment_history/delivery_variables.py`) and, from 2023, AORC
precipitation, rain and snow in every catchment, with ERA5-Land quantile-mapped
onto AORC:

```bash
catchment-history debias --aorc outputs/catchment_daily_1996_2025_full.csv --era5 outputs/catchment_daily_1996_2025_full.csv --output outputs/catchment_daily_1996_2025_mapped.parquet
```

Dates after the AORC archive come from `download --primary era5-land` and the
same mapping, joined with `merge`; catchments north of AORC (53°N) use
`download --primary era5-land` alone and are joined with `merge-deliveries`.
`scripts/daily_update.sh` appends the newly published days every day. The full
sequence is in the [pipeline guide](docs/pipeline.md).

## Commands

| Command | Does |
| --- | --- |
| `download` | AORC, ERA5-Land and ERA5 to the full table, its checks and the requested table |
| `check` | Checks of a full table, without downloading |
| `deliver` | The requested table and README from a full table |
| `debias` | ERA5-Land mapped onto AORC: gaps filled, `REPLACED` catchments replaced |
| `merge` | Join requested tables of consecutive periods; also appends to a table in place |
| `merge-deliveries` | Join requested tables of different catchments |
| `plot` | Daily series from a working directory or a table |
| `plot-debias` | AORC beside ERA5-Land before and after the mapping |
| `progress` | How far a running download has got |

`catchment-history COMMAND --help` lists the options of each.

## Catchment file

A WGS84 GeoJSON `FeatureCollection` of polygons, each with a unique
`properties.id` and `properties.part` (`local` for the area between an outlet
and the outlets upstream). Optional properties are described in
[catchment file](docs/pipeline.md#3-catchment-file).

## Documentation

- [Setting up and running](docs/running.md): host setup, building the table once
  and the daily update.
- [Pipeline guide](docs/pipeline.md): workflow, long runs, the AORC replacement,
  the 2026 extension, the daily update, the Peace River catchments, plots and
  variables.
- [Data notes](docs/data_notes.md): how to read the delivery, units, time
  conventions, missing values and the checks.

## Tests

```bash
python -m pip install -e '.[test]'
python -m pytest
```

The tests use small synthetic inputs and need no network or credentials.

## Data sources

- AORC v1.1, NOAA Office of Water Prediction, read from the public
  `noaa-nws-aorc-v1-1-1km` bucket on AWS.
- ERA5 and ERA5-Land, Copernicus Climate Change Service, from the Climate Data
  Store, Google's ARCO-ERA5 copy and the DestinE Earth Data Hub copy. Products
  derived from them must acknowledge the Copernicus Climate Change Service and
  follow the Copernicus licence.
