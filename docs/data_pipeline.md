# Historical catchment data pipeline

This guide covers the 1996-01-01 through 2025-12-31 daily data workflow for the
43 configured local catchments. It produces one row per project and UTC date,
with source-qualified variables, units and quality information. A complete
calendar contains 10,958 dates and 471,194 project-day rows. Missing values do
not remove dates from that calendar.

The boundaries use documented HydroSHEDS outlet cells and exclude virtual
endorheic connections. Kootenay Canal represents natural drainage at its
tailrace return. Its turbine inflow requires a separate operational diversion
model. See [project outlets](project_outlets.md).

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
- [ERA5 pressure levels](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-pressure-levels?tab=download)

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
  --start 1996-01-01 --end 2025-12-31 --chunk-days 31

python research/download_cds.py --product era5 \
  --geojson outputs/projects43_independent/dam_catchments.geojson \
  --output-dir data/era5_1996_2025 --cache-dir data/cds_cache \
  --start 1996-01-01 --end 2025-12-31 --chunk-days 31
```

CDS requests are split into bounded calendar batches; the default is seven
days. Larger batches reduce request overhead but require more temporary disk
space. Processing reads time slices rather than loading a 30-year cube.

Repeat an identical command to resume verified completed periods. AORC writes
annual files; CDS writes monthly files. Configuration and output hashes are
checked before reuse. Do not overwrite an old run after changing processing
code, geometry or options; use a new directory. Keep the source manifests with
the data. Raw AORC meteorological chunks are discarded after all requested
base and derived products consume them unless `--keep-chunks` is supplied.

## 6. Export one project-day row

After the required source periods have completed:

```bash
python research/export_daily.py \
  --geojson outputs/projects43_independent/dam_catchments.geojson \
  --input-dir data/aorc_1996_2025_derived data/era5_land_1996_2025 data/era5_1996_2025 \
  --start 1996-01-01 --end 2025-12-31 \
  --output outputs/delivery/catchment_daily_1996_2025.csv
```

Use `.csv.gz` for compressed CSV or `.parquet` for optional Parquet output.
The same command can export a short test period from the three demo directories.
The exporter requires completed, hash-verified source periods. It rejects
duplicate observations, conflicting units, different GeoJSON hashes and
mismatched project sets. Missing source periods are an error; a missing value
inside a completed period remains a flagged blank.

The unique key is `(date, project_id)`. Source-qualified columns prevent
overwrites, for example `aorc_v1_1__precipitation_mm` and
`era5_land_cds__precipitation_mm`. Each variable retains its own QC, valid
hours, expected hours and minimum valid-area fraction. The companion
`<output filename>.manifest.json` records the column dictionary, units,
aggregation definitions, input hashes and project metadata. Deliver it with
the data file. An internal destination is supplied through `--output`, not
hard-coded into the repository.

## Variables and calculations

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
| 2 m air temperature | `tmean_c` | Mean of 24 hourly catchment means, °C |
| Daily minimum/maximum temperature | `tmin_c`, `tmax_c` | Extrema of hourly catchment-mean temperature, °C; not averages of cellwise extrema |
| Specific humidity | `specific_humidity_kgkg` | Daily mean, kg/kg |
| Surface pressure | `surface_pressure_pa` | Daily mean, Pa |
| Shortwave radiation | `shortwave_down_mean_wm2`, `shortwave_down_energy_mjm2` | Incoming flux, W/m²; hourly integration estimate, MJ/m²/day |
| Longwave radiation | `longwave_down_mean_wm2`, `longwave_down_energy_mjm2` | Incoming flux, W/m²; hourly integration estimate, MJ/m²/day |
| U/V wind | `u_wind_ms`, `v_wind_ms` | Mean eastward/northward 10 m components, m/s |
| Wind speed | `wind_speed_ms` | Mean of native hourly `sqrt(u²+v²)`, m/s |
| Relative humidity | `relative_humidity_pct` | Native T/q/pressure calculation followed by averaging, % |
| Vapor pressure deficit | `vapor_pressure_deficit_kpa` | Native T/q/pressure calculation followed by averaging, kPa |
| Wet-bulb temperature | `wet_bulb_temperature_c` | Pressure-aware psychrometric calculation, °C |
| Rainfall and snowfall | `rainfall_mm`, `snowfall_mm` | Water-equivalent phase amounts, mm/day; sum to total precipitation when all inputs are valid |
| Potential evapotranspiration | `pet_hargreaves_mm` | Hargreaves-Samani estimate, mm/day |

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
about 9 km). The public NCAR three-variable demonstration remains available
in `download_era5_land.py`, but its tested subset does not provide this full
variable list or the entire requested period.

| Requested quantity | Output or calculation | Daily units and meaning |
| --- | --- | --- |
| Snow water equivalent | `snow_water_equivalent_mm` from `sd` | Daily mean water equivalent, mm |
| Physical snow depth | `snow_depth_m` from the separately identified `sde` field | Daily mean physical depth, m; never relabel `sd` as physical depth |
| Snow density and cover | `snow_density_kgm3`, `snow_cover_pct` | Supporting snow diagnostics, kg/m³ and % |
| Snowmelt | `snowmelt_mm` | Modeled water-equivalent melt, mm/day |
| Surface soil moisture | `soil_moisture_layer1_m3m3` | Volumetric water content, 0-7 cm, m³/m³ |
| Root-zone soil moisture | `root_zone_soil_moisture_0_100cm_m3m3` | Defined here as the 0-100 cm thickness-weighted mean |
| Deep soil moisture | `soil_moisture_layer4_m3m3` | Volumetric water content, 100-289 cm, m³/m³ |
| Soil temperature | `soil_temperature_layer1_c` through `soil_temperature_layer4_c` | Separate daily means for the four layers, °C |
| Actual evapotranspiration | `actual_evapotranspiration_mm` from total evaporation | mm/day, positive upward water loss; negative values represent net deposition/condensation |
| Net shortwave/longwave radiation | `net_shortwave_energy_mjm2`, `net_longwave_energy_mjm2` | Accumulated net energy, MJ/m²/day |
| Total net radiation | `net_radiation_energy_mjm2`, `net_radiation_mean_wm2` | Net solar plus net thermal energy, MJ/m²/day, and equivalent mean flux, W/m² |
| Temperature and precipitation | `tmean_c`, `tmin_c`, `tmax_c`, `precipitation_mm` | Source-qualified comparison fields; same temperature-statistic definitions; precipitation in mm/day |

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
supplies total cloud cover, surface pressure and surface geopotential. The
[pressure-level product](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-pressure-levels)
supplies temperature and geopotential through the lower/middle atmosphere.

`cloud_cover_fraction` is a daily mean fraction, 0-1. Freezing-level processing masks
pressure levels below the local surface and interpolates the lowest upward
warm-to-cold 0°C crossing. No crossing and multiple-crossing profiles are
flagged rather than extrapolated. The output retains both
`freezing_level_geopotential_height_m`, relative to the reference geoid, and
`freezing_level_above_terrain_m`, relative to local terrain. The requested
"0°C isotherm elevation" is this same diagnostic with the vertical reference
made explicit; it is not a second independent measurement.

The combined output contains **45 data variables**: 19 from AORC with
`--derive`, 23 from ERA5-Land and 3 from ERA5. Each has four companion quality
columns. Together with `date` and `project_id`, the full table has 227 columns.
Daily accumulated quantities use `mm` or `MJ/m2` in the machine-readable unit
dictionary; the daily interval makes them numerically equivalent to the
mm/day and MJ/m²/day amounts in the tables above.

## Code reference

| File | Purpose |
| --- | --- |
| `research/download_aorc.py` | Retrieve native AORC fields and calculate polygon series; `--derive` enables diagnostics |
| `research/meteorology.py` | Humidity, wet bulb, rain/snow partition, wind speed and Hargreaves PET formulas |
| `research/aggregate_daily.py` | Aggregate hourly polygon series with explicit UTC timing and coverage rules |
| `research/download_cds.py` | Retrieve ERA5-Land/ERA5, calculate polygon statistics and write monthly daily files |
| `research/cds_fields.py` | CDS variable definitions, soil weighting, conversions and freezing-level calculations |
| `research/export_daily.py` | Verify source artifacts and export the final daily CSV or Parquet table |

For modeling, begin with AORC precipitation and temperature as meteorological
forcing. Rain/snow partition and PET are calculated inputs with the method
defined here. Use the ERA5-Land states and fluxes only where your model needs
external states, predictors or evaluation data; do not count externally
supplied snowmelt/ET a second time if the hydrological model already computes
those processes. Preserve the source and QC columns when preparing model
inputs. Missing or flagged values require a separately chosen treatment.

## Quality, interpretation and validation

Validation on 2026-10-06 passed 76 core tests and 101 research tests, including
all three source-processing contracts through the final exporter across a
December/January boundary using synthetic CDS responses. A real cached AORC
replay for 43 projects over 2025-12-29 through 2025-12-31 produced 2,451 daily
records: 2,322 valid and 129 flagged precipitation/rain/snow values on the
last date. Rain plus snow matched precipitation in all 86 complete project-days
within 1.1e-13 mm. CSV and Parquet exports both contained 129 rows. The CDS
request previews cover all 43 projects, but real authenticated retrieval has
not yet been verified.

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
