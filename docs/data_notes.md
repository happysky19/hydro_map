# Daily data notes

**AORC wind is 10 m above ground. Air temperature and specific humidity are
2 m above ground.** These are gridded estimates, not measurements from a
single sensor at a dam. The heights describe the source fields before
catchment averaging. See the [NOAA AORC methods, section 3](https://www.weather.gov/media/owp/operations/aorc_v1_1_methods.pdf).

## Read the delivery

Each row describes one project and one **UTC calendar day**. All values are
area-weighted over the supplied local catchment. They are not dam-point
values or totals over all upstream catchments. The values file, `_qc` file
and `.manifest.json` belong together. Empty values are missing, not zero.
The manifest identifies each column's source, units and temporal statistic.

The original `download_daily.py` command now also produces an `OUTPUT.checks/`
directory. To check an existing delivery without downloading:

```bash
python research/check_daily.py outputs/catchment_daily.csv
```

CSV, compressed CSV and Parquet are supported. The QC table and manifest must
remain beside the values file. This does not alter the inputs.

| Check output | Use |
| --- | --- |
| `coverage.png` | Percentage of valid days for every variable and project |
| `source_comparison.png` | AORC versus ERA5-Land precipitation and temperature; the dashed line is equality |
| `timeseries.pdf` | One page per project: temperature, precipitation, radiation, SWE, 10 m wind and freezing height |
| `checks.csv` | Algebraic identities, physical bounds, QC/value agreement and examples of failures |
| `variables.csv` | Per-project coverage, minimum, mean and maximum for every variable |
| `qc_summary.csv` | Per-project counts of each variable's exact QC flag, including freezing-height reasons |
| `source_comparison.csv` | Paired-day count, bias (ERA5-Land minus AORC), MAE, RMSE and correlation by project and pooled across projects |
| `summary.json` | Input hashes, checker version, missing-value count and check results |

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

### ERA5-Land: land states and fluxes (23 columns)

These are modeled land-surface quantities from the
[CDS ERA5-Land product](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land).

| Column suffix | Meaning |
| --- | --- |
| `tmin_degC`, `tmean_degC`, `tmax_degC`, `precipitation_mm` | Comparison fields, with the same daily-statistic definitions as above |
| `snow_water_equivalent_mm` | Mean snow water storage, from `sd` |
| `snow_depth_m` | Mean physical snow depth, from the separate `sde` field |
| `snow_density_kg_m3`, `snow_cover_pct` | Mean snow density and snow-covered percentage |
| `snowmelt_mm` | Daily modeled melt, in water equivalent |
| `soil_moisture_layer1_m3_m3` through `soil_moisture_layer4_m3_m3` | Mean volumetric water content in 0–7, 7–28, 28–100 and 100–289 cm layers |
| `soil_temperature_layer1_degC` through `soil_temperature_layer4_degC` | Mean temperature in those same four layers |
| `root_zone_soil_moisture_0_100cm_m3_m3` | Defined here as `0.07*layer1 + 0.21*layer2 + 0.72*layer3` |
| `actual_evapotranspiration_mm` | Daily total evaporation converted to positive upward water loss; includes sublimation; negative means net deposition/condensation |
| `net_shortwave_energy_MJ_m2`, `net_longwave_energy_MJ_m2` | Daily net solar/thermal energy, positive toward the surface |
| `net_radiation_energy_MJ_m2`, `net_radiation_mean_W_m2` | Net shortwave plus net longwave, as daily energy and equivalent mean flux |

SWE and physical snow depth are different quantities. Multiplying separately
averaged snow density and depth will not generally reproduce mean SWE.
Warm soil beneath freezing air is possible: soil stores heat, and snow changes
heat exchange. Small amounts of melt can occur while daily mean air temperature
is below freezing; the averaging removes hourly and spatial variability.

### ERA5: cloud and profile diagnostics (3 columns)

| Column suffix | Meaning |
| --- | --- |
| `cloud_cover_fraction` | Mean total cloud fraction, 0–1; this is not a percentage |
| `freezing_level_geopotential_height_m` | Height of the single bracketed warm-to-cold 0°C crossing, relative to the reference geoid |
| `freezing_level_above_terrain_m` | That height minus local terrain geopotential height, before spatial averaging |

Clouds come from [ERA5 single levels](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels).
Freezing height is derived here from [ERA5 pressure-level temperature and geopotential](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-pressure-levels),
using levels from 1000 to 300 hPa. Levels below terrain are excluded. The
method does not extrapolate outside an above-ground bracket or choose among
multiple crossings. A fully subfreezing profile has no such crossing; that is
not equivalent to a valid freezing height of zero metres.

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
not renormalized. For freezing height, this is intentionally strict: one
undefined contributing cell/hour can invalidate the basin day. Consult
`qc_summary.csv`, then the dated `_qc` table. `no_crossing`, `multiple_crossings`,
`missing_profile` and `missing_surface` describe different issues. Aggregated
reason flags identify conditions encountered, not the number of grid cells
having each condition. Never replace these blanks with zero without a separate,
documented modeling decision.

## Interpret the checks

- **15.5 MJ/m²/day of downward longwave is about 179.4 W/m².** That conversion
  is correct and does not establish an impossible value. Atmospheric emission
  depends on temperature, humidity and cloud; an effective sky temperature need
  not equal near-surface air temperature. Cold, dry conditions can have low
  downward thermal flux. [FAO explains sky emission and radiative frost](https://www.fao.org/4/y7223e/y7223e09.htm).
  Verify the original hourly values and support before judging a particular day.
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

The checker verifies delivery hashes, matching keys and the complete daily
calendar. It counts missing values separately from violated identities. Missing
values alone do not fail the command; review their coverage before training.
Exit status 2 indicates failed consistency checks; status 1 indicates an
unreadable or inconsistent delivery. The combined downloader also returns 2 if
post-export diagnostics fail; its saved data remain available for inspection.
No check fills, rescales or bias-corrects the inputs. Independent validation
against suitable station, snow or basin observations is a separate step.
