# Forecast data: limitations and fitting

Checked 2026-10-14, for inflow models trained on the 1996-2025 catchment table.
Sources and access for each system are in
[forecast hindcasts](forecast_hindcast_plan.md).

## Limitations

### Variables

| System | Not available | Approximation only |
| --- | --- | --- |
| GEFS v12 | – | Snow depth (SWE only); snowmelt (snowfall minus the change in SWE); freezing level (from pressure levels); soil layers differ from ERA5-Land |
| ECMWF IFS ENS, S2S reforecasts (1.5°) | Deep soil moisture (100-289 cm) | RH, VPD and the rain/snow split from daily-mean 2 m temperature and dewpoint; soil moisture for 0-20 and 0-100 cm; soil temperature at two depths; snowmelt; freezing level from the control member only |
| ECMWF IFS ENS, open data (0.25°) | AET; snowmelt; SWE, snow depth and cloud cover before 2025-11-21 | Freezing level |
| ECMWF AIFS v2 | SWE; snow depth; snowmelt; AET; soil moisture below 28 cm; soil temperature layers 3-4 | Net radiation; Tmax and Tmin from 6-hourly values |
| WeatherNext 2 | Shortwave and net radiation; cloud cover; every snow and soil variable; AET | 2 m humidity from pressure levels, hence RH, VPD and the rain/snow split; Tmax and Tmin from 6-hourly values; freezing level |
| ECCC GEPS 8 | Root-zone and deep soil moisture; soil temperature layers 2-4 | Soil moisture and temperature for 0-10 cm only; snowmelt; freezing level. The S2S reforecast has fewer fields than the real-time output |
| ECCC HRDPS, RDPS, REPS, GDPS | Similar to GEPS; to be checked per product | Similar to GEPS |
| ECCC GEML | Precipitation; 2 m humidity; radiation; cloud cover; snow; soil | Not usable on its own |
| C3S seasonal systems, including CanSIPS | Root-zone and deep soil moisture; soil temperature layers 2-4 | Snowmelt; freezing level; one soil moisture layer; grid-point daily Tmax and Tmin |

- Every usable system provides precipitation, 2 m temperature (mean, maximum,
  minimum) and 10 m wind. PET follows from temperature. These are the inputs
  common to all systems.
- A variable that exists can still differ in definition from the historical
  column:
  - 6-hourly output (AIFS, WeatherNext 2, C3S) gives four values a day, so its
    daily mean and extremes differ from those of 24 hourly values;
  - Tmax and Tmin in GEFS, IFS, GEPS and C3S are grid-point extremes, which are
    more extreme than the extremes of the hourly catchment mean used in the
    table;
  - each model has its own snow scheme and soil layers, so its SWE and soil
    moisture are not the ERA5-Land quantities in the table;
  - half of the catchments are smaller than 5,250 km2, below the area of one 1°
    cell (about 8,400 km2 at 47°N), so on the 1° and 1.5° grids each catchment
    value comes from one or a few cells.
- SWE, snow depth and soil moisture are therefore taken at the start date from
  the historical table, not from the forecasts. Snowmelt and AET from the
  forecast models are not used.

### Hindcast time scales

| System | Leads | Hindcast years within 1996-2025 | Members and starts | Grid |
| --- | --- | --- | --- | --- |
| GEFS v12 | 1-16 days; 35 days on Wednesdays | Reforecast 2000-2019; operational forecasts of the same model 2020-09 to 2025 | 5 daily at 00 UTC; 11 on Wednesdays | 0.25° to day 10, 0.5° after |
| ECMWF IFS ENS, S2S reforecasts | 1-46 days | The 20 years before each start date: about 2004-2025 for cycle 49r1 | 11 | 1.5° |
| ECMWF IFS ENS, open data | 1-15 days | 2024-03-06 to 2025-12-31 | 51, twice a day | 0.25° |
| ECCC GEPS 8 | 1-39 days | 2001-2020 | 4, Mondays and Thursdays | 1.5° |
| WeatherNext 2 | 1-15 days | 2022-2025 | 64, four starts a day | 0.25° |
| ECCC HRDPS, RDPS, REPS, GDPS | 2-10 days | Real-time archive from 2015 or 2017, through several model versions | As run | 2.5-15 km |
| ECMWF AIFS v2 | 1-15 days | None: operational from 2026-05-12. Runs from ERA5 are within its training period before 2025 | – | 0.25° |
| C3S seasonal | Up to 184-217 days | 1993-2016 for every system; later for some (Météo-France to 2024, DWD and ECCC to 2023, CMCC to 2022, JMA to 2020, BOM to 2018); SEAS5 from 1981 | 3-30 per start; mostly one start a month | 1° |

- Leads beyond 46 days exist only in the seasonal systems. With one start a
  month, each start month has one start date a year: 21 to 29 years for a fit
  over 1996 onward, depending on the system. This is the main limit on seasonal
  fits.
- The historical table ends on 2025-12-31 and AORC for 2026 is not released, so
  no forecast after 2025 can be fitted or verified against it. AIFS v2 and the
  hybrid GDPS have no data before 2026.
- WeatherNext 2 (four years), IFS at 0.25° (under two years) and the ECCC
  archives (nine to eleven years, with upgrades) are too short or too
  inhomogeneous for detailed fits.
- GEFS v12 is frozen. ECMWF reruns its reforecasts with each model cycle, so a
  fit belongs to one cycle. GEPS runs its reforecasts with the operational
  model.
- Members of one start are not independent: the effective sample is closer to
  the number of start dates than to the number of members.

## Fitting

Each forecast is fitted to the historical table, which is the climate the
inflow model was trained on, not to an outside truth.

1. **Pairs.** Each forecast is aggregated exactly as the table: same polygons
   and fractional weights, UTC days, sub-daily values averaged before daily
   statistics, accumulations differenced between steps. A pair is one member's
   daily catchment value and the table's value for the same catchment and valid
   date, kept with its start date and lead.
2. **Strata.** One fit per catchment, calendar window (±15 days around the
   target date, across years) and lead group: single days for days 1-3, then
   days 4-7, weeks up to day 46, and months beyond. Neighbouring catchments are
   pooled only where the record is too short.
3. **Methods.**
   - Temperature (mean, maximum, minimum) and wind speed: quantile mapping of the
     members against the table's distribution. This also removes the systematic
     difference of 6-hourly extremes. For short records, a correction of mean
     and variance only.
   - Precipitation: quantile mapping after matching the frequency of dry days,
     or a censored, shifted gamma regression on the ensemble mean where a
     calibrated spread is needed (Scheuerer and Hamill 2015).
   - Humidity and radiation, where used: quantile mapping within physical
     bounds.
   - Rainfall, snowfall, RH, VPD and PET are recomputed after the fit from the
     corrected precipitation and temperature with the table's formulas, so that
     rainfall plus snowfall still equals precipitation.
4. **Ensemble structure.** Correcting each day, catchment and variable on its
   own breaks persistence in time and the dependence between catchments and
   variables. The corrected members are reordered with the Schaake shuffle
   (Clark et al. 2004), using sequences of the table from the same calendar
   window.
5. **Seasonal leads (60-180 days).** Monthly means and totals are fitted, not
   daily values. The corrected monthly forecast either weights the historical
   years of the table run from the current snow and soil, or scales the daily
   members. Both are compared with the unweighted historical years, the
   reference a seasonal forecast has to beat.
6. **Validation.** Leave one year out: fit on every other year, apply to the
   left-out year. Report CRPS skill against climatology, bias and rank
   histograms by catchment, month and lead. A system is used only for the leads
   where its skill is positive.
7. **Short or changing records.**
   - IFS at 0.25°: fit on the 1.5° reforecasts, with a resolution adjustment
     estimated on the 2024-2025 overlap.
   - WeatherNext 2 and the ECCC archives: pooled months and leads, mean and
     variance only; refit after each model upgrade.
   - ECMWF: refit with each new cycle on that cycle's reforecasts.
8. **Start-date states.** SWE and soil moisture are not taken from the
   forecasts. In hindcasts they come from the table; in real time from a
   substitute (ERA5T, the IFS analysis, CaPA and CaLDAS in Canada) fitted to the
   table on their common years with the same quantile mapping.

## References

- Clark, M. et al. (2004). The Schaake shuffle: a method for reconstructing
  space-time variability in forecasted precipitation and temperature fields.
  *Journal of Hydrometeorology* 5, 243-262.
- Scheuerer, M. and Hamill, T. M. (2015). Statistical postprocessing of ensemble
  precipitation forecasts by fitting censored, shifted gamma distributions.
  *Monthly Weather Review* 143, 4578-4596.
