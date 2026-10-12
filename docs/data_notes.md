# Daily data notes

**AORC wind is 10 m above ground. Air temperature and specific humidity are
2 m above ground.** These are gridded estimates, not measurements from a
single sensor at a dam. The heights describe the source fields before
catchment averaging. See the [NOAA AORC methods, section 3](https://www.weather.gov/media/owp/operations/aorc_v1_1_methods.pdf).

## Read the delivery

Each row describes one project and one **UTC calendar day**. All values are
area-weighted over the supplied local catchment. They are not dam-point
values or totals over all upstream catchments. Empty values are missing, not zero.

`OUTPUT.csv` holds the requested variables and `OUTPUT_README.md` explains
each of them: source, native or computed, units, daily definition and the
matching ECMWF IFS, NOAA GFS and WeatherNext 2 forecast fields. `OUTPUT_full.csv`
holds every variable from every source; its `_qc` file, `.manifest.json` and
`.checks/` directory belong with it. The manifest identifies each column's
source, units and temporal statistic. To check an existing delivery and
rebuild the requested table and README without downloading:

```bash
python research/check_daily.py outputs/catchment_daily_full.csv
python research/deliver_daily.py outputs/catchment_daily_full.csv
```

CSV, compressed CSV and Parquet are supported. The QC table and manifest must
remain beside the full values file. This does not alter the inputs.

| Check output | Use |
| --- | --- |
| `coverage.png` | Percentage of valid days for every variable and project |
| `source_comparison.png` | AORC versus ERA5-Land for every quantity both provide; the dashed line is equality |
| `timeseries.pdf` | One page per project: temperature, precipitation, radiation, SWE, 10 m wind and the 0°C level |
| `checks.csv` | Algebraic identities, plausible ranges, QC/value agreement and examples of failures |
| `variables.csv` | Per-project coverage, minimum, mean and maximum for every variable |
| `qc_summary.csv` | Per-project counts of each variable's exact QC flag |
| `source_comparison.csv` | Paired-day count, means, bias (ERA5-Land minus AORC), mean ratio for nonnegative quantities, MAE, RMSE and correlation by project and pooled |
| `summary.json` | Input hashes, checker version, blank values by column with QC reasons, catchments with year-round SWE, review-flag counts and check results |
| `outliers.csv` | Values within the plausible ranges that are unusual and worth a look, most unusual first (see below) |
| `outliers.pdf` | The 24 most unusual events, each with 30 days either side, the other source and the nearest catchments |

Time-series plots show daily values for windows up to 366 days. Longer windows
show monthly means of available valid daily values, including mean daily
precipitation, **not monthly precipitation totals**. Missing months remain gaps.
Scatter plots display at most 10,000 regularly sampled pairs per panel; metrics
use every valid pair. The `ALL` comparison pools project-days and is not an
independent estimate of model skill. Correlation is blank with fewer than three
pairs or a constant series. A three-day comparison cannot establish a seasonal
or long-term bias. Charts have no footnotes.

## Variables and units

Names below omit the source prefix: `aorc_v1_1__`, `era5_land_cds__` or
`era5_cds__`. A daily amount ending in `_mm` or `_MJ_m2` is the amount in that
UTC day; it is numerically the same as mm/day or MJ/m²/day. State variables
such as SWE are stocks, not daily increments.

### AORC: forcing and derived quantities (19 columns)

The eight original fields are documented in the
[NOAA archive](https://registry.opendata.aws/noaa-nws-aorc/).
Rain/snow, humidity diagnostics, scalar wind speed and PET are calculated by
this repository; they are not additional independently observed AORC fields.

| Column suffix | Meaning |
| --- | --- |
| `precipitation_mm` | Daily total water equivalent, rain plus snow |
| `tmin_degC`, `tmean_degC`, `tmax_degC` | Minimum, mean and maximum of 24 hourly catchment-mean **2 m** temperatures |
| `specific_humidity_kg_kg` | Mean **2 m** water-vapor mass fraction |
| `surface_pressure_Pa` | Mean terrain-level pressure; not sea-level pressure |
| `u_wind_m_s`, `v_wind_m_s` | Mean **10 m** components; positive eastward and northward, respectively |
| `wind_speed_m_s` | Mean **10 m** scalar speed: compute `sqrt(u²+v²)` at each native cell/hour, then average |
| `shortwave_down_mean_W_m2`, `longwave_down_mean_W_m2` | Mean incoming solar/thermal flux at the surface |
| `shortwave_down_energy_MJ_m2`, `longwave_down_energy_MJ_m2` | Incoming energy integrated over the day |
| `relative_humidity_pct` | Relative humidity calculated from native 2 m temperature/humidity and surface pressure; saturation is referenced to liquid water |
| `vapor_pressure_deficit_kPa` | Mean nonnegative saturation deficit, calculated before averaging |
| `wet_bulb_temperature_degC` | Mean pressure-aware psychrometric estimate; liquid-water formulation even below freezing |
| `rainfall_mm`, `snowfall_mm` | Diagnostic phase split, in water equivalent; native wet bulb ≤0.5°C is snow, otherwise rain |
| `pet_hargreaves_mm_day` | Hargreaves-Samani estimate using basin temperature statistics, centroid latitude and date; negative estimates clipped to zero |

U and V can be negative. Mean scalar speed generally exceeds the magnitude
of the mean vector because directions vary across space and time. If a model
requires 2 m wind, convert explicitly using the chosen surface assumptions;
do not relabel these 10 m data. The current Hargreaves calculation does not
use wind. [FAO-56 describes wind-height conversion and reference ET assumptions](https://www.fao.org/4/X0490E/x0490e07.htm).

### ERA5-Land: land states, fluxes and forcing counterparts (40 columns)

These are modeled land-surface quantities from the
[CDS ERA5-Land product](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land).

| Column suffix | Meaning |
| --- | --- |
| `tmin_degC`, `tmean_degC`, `tmax_degC`, `precipitation_mm` | Comparison fields, with the same daily-statistic definitions as above |
| `specific_humidity_kg_kg`, `relative_humidity_pct`, `vapor_pressure_deficit_kPa`, `wet_bulb_temperature_degC` | From 2 m dewpoint, 2 m temperature and surface pressure in every cell and hour, with the AORC formulas, then averaged |
| `surface_pressure_Pa`, `u_wind_m_s`, `v_wind_m_s`, `wind_speed_m_s` | As for AORC; speed from cell-hour `u10`, `v10` |
| `snowfall_mm`, `rainfall_mm` | Native model snowfall `sf`; rainfall is total precipitation minus snowfall. Not the AORC wet-bulb rule |
| `shortwave_down_mean_W_m2`, `longwave_down_mean_W_m2` and their `_energy_MJ_m2` | 24-hour accumulated downward radiation (`ssrd`, `strd`) |
| `snow_water_equivalent_mm` | Mean seasonal snow water storage, from `sd`, excluding perennial-snow cells (below) |
| `snow_depth_m` | Mean physical snow depth, from the separate `sde` field |
| `snow_density_kg_m3` | Mean density of the snow-covered seasonal-snow cells (SWE ≥ 1 mm); blank (`no_defined_area`) when the catchment has no snow |
| `snow_cover_pct` | Mean snow-covered percentage of the seasonal-snow area |
| `perennial_snow_area_pct` | Share of the catchment in ERA5-Land cells holding at least 5 m water equivalent all day (glaciers), which the snow states exclude |
| `snowmelt_mm` | Daily modeled melt, in water equivalent |
| `soil_moisture_layer1_m3_m3` through `soil_moisture_layer4_m3_m3` | Mean volumetric water content in 0–7, 7–28, 28–100 and 100–289 cm layers |
| `soil_temperature_layer1_degC` through `soil_temperature_layer4_degC` | Mean temperature in those same four layers |
| `root_zone_soil_moisture_0_100cm_m3_m3` | Defined here as `0.07*layer1 + 0.21*layer2 + 0.72*layer3` |
| `actual_evapotranspiration_mm` | Daily total evaporation converted to positive upward water loss; includes sublimation; negative means net deposition/condensation |
| `net_shortwave_energy_MJ_m2`, `net_longwave_energy_MJ_m2` and `_mean_W_m2` | Daily net solar/thermal energy and equivalent flux, positive toward the surface |
| `net_radiation_energy_MJ_m2`, `net_radiation_mean_W_m2` | Net shortwave plus net longwave, as daily energy and equivalent mean flux |

ERA5-Land accumulates snow without limit on its glacier cells: in the Mica
catchment, 1.7% of the area holds 6–9.5 m water equivalent, which would raise
catchment SWE on 2024-01-01 from 200 to 324 mm.
SWE, snow depth, snow density and snow cover therefore describe the seasonal
snowpack over the remaining area; `perennial_snow_area_pct` gives the excluded
share (zero in the other 42 catchments). Snowmelt still includes those cells.
ERA5-Land also stores a nominal 100 kg/m³ density on snow-free cells, so
density is averaged only where snow exists.

SWE and physical snow depth are different quantities. Multiplying separately
averaged snow density and depth will not generally reproduce mean SWE.
Warm soil beneath freezing air is possible: soil stores heat, and snow changes
heat exchange. Small amounts of melt can occur while daily mean air temperature
is below freezing; the averaging removes hourly and spatial variability.

### ERA5: cloud cover and the 0°C level (3 columns)

| Column suffix | Meaning |
| --- | --- |
| `cloud_cover_fraction` | Mean total cloud fraction, 0–1; this is not a percentage |
| `freezing_level_above_ground_m` | Native ECMWF `zero_degree_level` (`deg0l`): height above the model surface where temperature passes from positive to negative |
| `freezing_level_above_sea_level_m` | `deg0l + z/9.80665` in every cell and hour: the 0°C isotherm elevation |

All three come from [ERA5 single levels](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels).
ECMWF computes `deg0l` on its 137 model levels. It is **zero when the whole
column is below 0°C**, so a cold day lowers the daily mean instead of making it
blank. With more than one warm layer, ECMWF assigns the top of the second
atmospheric layer ([parameter 228024](https://codes.ecmwf.int/grib/param-db/228024)).
The above-sea-level column adds ERA5's surface geopotential height, so on a
fully frozen hour it equals the model terrain height of that cell. Both are
defined everywhere and normally have no blanks. Model terrain at 0.25° is
smoother than the real catchment; compare the above-sea-level height with the
catchment's elevation range, not with a single summit or valley station.

An earlier version derived the freezing level from 20 pressure levels and left
any project-day blank if a single cell-hour lacked exactly one crossing; it
produced no valid days in winter tests and has been replaced.

## Time, spatial support and missing values

For AORC, state/flux samples use 00–23 UTC. Hour-ending precipitation uses
01 UTC through the following 00 UTC. Temperature extrema are extrema of
hourly basin means, not spatial means of each grid cell's daily extrema.
Radiation energy is a rectangular hourly integral:

```text
energy_MJ_m2 = sum(hourly_flux_W_m2) * 3600 / 1,000,000
energy_MJ_m2 = daily_mean_flux_W_m2 * 0.0864
```

ERA5-Land states use 24 hourly samples. Accumulated fields use the following
midnight's 24-hour forecast endpoint, converted once; cumulative hourly values
are not summed together. For these endpoints, `valid_hours=24` describes the
represented duration, not 24 separate downloaded measurements.
[ECMWF documents these accumulation conventions](https://confluence.ecmwf.int/pages/viewpage.action?pageId=501029136).

A valid daily value requires full contributing-area coverage and all required
hours. Missing cells are not silently removed and the remaining weights are
not renormalized. Consult the `missing_by_column` entry of `summary.json` or
`qc_summary.csv`, then the dated `_qc` table. `missing_hours` and
`invalid_hours` mean the source lacks some required hours or cells;
`missing_record` means no record exists for that project-day. Never replace
these blanks with zero without a separate, documented modeling decision.

AORC precipitation for the last day of a period needs the next day's 00 UTC
hour. The AORC archive ends at 2025-12-31 23 UTC, so precipitation, rainfall
and snowfall on 2025-12-31 are blank (`missing_hours`, 23 valid hours). AORC
v1.1 also has no data at all for 2024-06-18 (00-23 UTC): both the Zarr archive
and NOAA's NWRFC NetCDF files contain only fill values for that day, across the
whole domain. That day is blank for every AORC variable, and precipitation is
also blank on 2024-06-17 because its last hour ends at 2024-06-18 00 UTC.

These blanks stay blank in the full table. In the requested table they are
filled from the same ERA5-Land quantity, adjusted to AORC's mean for that
catchment and calendar month (a shift for temperatures and relative humidity;
a ratio limited to 0.25-4 for precipitation, shortwave radiation and vapor
pressure deficit). Filled precipitation is split
with ERA5-Land's snow fraction so that rain plus snow still equals the total,
and PET is recalculated from the filled temperatures. The `aorc_gap_filled`
column lists the filled columns of each row, and the README counts them.

AORC's 10 m wind changes source at the start of 2018, from NLDAS-2 (12 km,
itself from the 32 km NARR reanalysis) to the 2.5 km URMA analysis, which is
anchored to surface stations; no adjustment joins the two
([AORC v1.1 methods](https://www.weather.gov/media/owp/operations/aorc_v1_1_methods.pdf),
which give 2016 for the change). Relative to ERA5-Land, AORC wind speed at the
43 catchments drops by about 1.5-3 m/s from 2018, and the checker's `step`
flags show it in nearly every catchment. The requested table therefore takes
wind speed and its components from ERA5-Land, which uses ERA5's 31 km 10 m
wind throughout: consistent over time, but smooth and likely low in complex
terrain ([Chen et al. 2024](https://rmets.onlinelibrary.wiley.com/doi/10.1002/joc.8339)).
The full table keeps both sources.

## Interpret the checks

- **15.5 MJ/m²/day of downward longwave is about 179.4 W/m².** That conversion
  is correct and does not establish an impossible value. Atmospheric emission
  depends on temperature, humidity and cloud; an effective sky temperature need
  not equal near-surface air temperature. Cold, dry conditions can have low
  downward thermal flux. [FAO explains sky emission and radiative frost](https://www.fao.org/4/y7223e/y7223e09.htm).
  Verify the original hourly values and support before judging a particular day.
- ERA5 stores "no 0 °C level above ground" (the whole column below freezing) as
  -0.0002 m rather than 0, an artifact of GRIB packing; the bound accepts values
  down to -0.001 m and such values mean zero.
- Incoming AORC longwave and ERA5-Land **net** longwave are different quantities.
  Negative net longwave, or even negative daily total net radiation in winter,
  can represent surface energy loss. They are not negative incoming sunlight.
- AET from one model can exceed an uncalibrated Hargreaves estimate from another
  forcing source. The checker does not impose `AET <= PET`. These columns are
  not a matched actual/potential pair with identical surface assumptions.
- Precipitation differences between sources need investigation, particularly
  over mountains. Compare paired valid days, absolute differences and seasonal
  totals before ratios: ratios near zero exaggerate small differences. Neither
  source is designated as observed truth by this comparison.
- Algebraic checks use `5e-5 + 1e-6*abs(reference)` tolerance in the reported
  units. Bounds check broad physical constraints, not regional climatological
  plausibility. RH above 100% is retained as in the source-derived calculation.
  A passing check is not proof of accurate weather, boundaries, reservoir
  operations or river discharge.

- AORC and ERA5-Land are independent estimates of the shared quantities. The
  README and `source_comparison.csv` give their bias, mean ratio and
  correlation. A large difference is not by itself an error in either source;
  physically impossible values fail the plausibility checks instead.

### Review flags

A value can pass every plausibility bound and still be wrong, for example
120 mm of rain in one catchment while its neighbours and the other source stay
dry. `outliers.csv` lists such values for a person to judge; they do not fail
the check and nothing is changed. `score` is 1 at the threshold and larger for
more unusual values; `reference` is what the value was compared with.

| Kind | Flagged when |
| --- | --- |
| `climatology` | Far from the catchment's own values for the same half-month and the two either side, across all years: more than 6 robust standard deviations for states, or above both 20 mm and three times the 99th percentile of wet days for amounts |
| `spike` | One day out of line with both the day before and the day after, by more than 8 robust standard deviations of day-to-day change; air temperature, wet bulb and surface pressure only |
| `snow` | ERA5-Land SWE rises by more than two days of precipitation plus 15 mm, or falls by more than two days of snowmelt plus 20 mm |
| `sources` | AORC and ERA5-Land differ far more than usual for that catchment and time of year, or one has at least 25 mm and five times the other |
| `neighbours` | A catchment differs far more than usual from the median of its three nearest catchments (by outlet), or has at least 30 mm while none of them has a fifth of it |
| `step` | The annual mean AORC minus ERA5-Land difference shifts lastingly from one year (at least three years either side), such as at a change of AORC input data |
| `constant` | The same nonzero value of a weather variable on five or more consecutive days |

Snow states and the 0 °C level sit at a floor for long spells (zero snow; a
0 °C level of zero, or at the terrain height above sea level, in cold air) and
leave it in real events, so climatology, neighbours and runs of equal values
mislead for them: snow is judged by the `snow` rule and the 0 °C level by its
bounds. Soil variables are not judged day against day: rain makes a one-day
jump in soil moisture, and deep soil changes by less than the stored precision
for days.
Statistics that need the seasonal cycle require at least a year of data. Start
with the top of the list or `outliers.pdf`: a real storm, heat wave or cold
outbreak usually appears in both sources and the neighbours, while a data
problem is confined to one source, one catchment or one day.

The checker verifies delivery hashes, matching keys and the complete daily
calendar. It counts missing values separately from violated identities. Missing
values alone do not fail the command; review their coverage before training.
Exit status 2 indicates failed consistency checks; status 1 indicates an
unreadable or inconsistent delivery. The combined downloader also returns 2 if
post-export diagnostics fail; its saved data remain available for inspection.
No check fills, rescales or bias-corrects the inputs. Independent validation
against suitable station, snow or basin observations is a separate step.
