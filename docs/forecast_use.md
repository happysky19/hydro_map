# Using forecast data for the inflow models

Written 2026-10-14. Which forecasts exist, their hindcasts and their variables
are in [forecast hindcasts](forecast_hindcast_plan.md). This note is about how
to use them.

In short:

1. Train the inflow model on the historical table, drive it with bias-corrected
   ensemble forecasts, and get an inflow ensemble. Training on forecast inputs
   comes later, and only for systems with long, unchanged hindcasts.
2. The source of inflow skill changes with lead time: forecast weather for the
   first one to two weeks, snow and soil at the start date for months ahead.
   Each lead range is treated differently.
3. A forecast system is used only where its hindcasts beat ensemble streamflow
   prediction (ESP: past weather years run from today's snow and soil) for the
   same catchment, month and lead.
4. Forecasts are corrected toward the historical table, the climate the inflow
   model was trained on, not toward an outside "truth".
5. The model inputs are the variables every chosen system provides;
   snow and soil enter only at the start date.
6. Start with the four systems that have long hindcasts: GEFS v12, ECMWF
   (S2S reforecasts), GEPS 8 and the C3S seasonal systems.

## Where the skill comes from

- **Days 1-10.** Inflow depends on how much precipitation falls, when, and
  whether as rain or snow, and on temperature for melt. Forecast weather adds
  most of the skill here, and daily values matter.
- **Weeks 2-6.** Day-to-day weather is no longer predictable, but weekly
  temperature and precipitation anomalies still carry some skill, which matters
  for the timing of snowmelt.
- **Months 2-6.** In snowmelt catchments like these, most of the predictable
  inflow volume is already on the ground as SWE and soil moisture at the start
  date. Seasonal forecasts add mainly a temperature signal; their precipitation
  skill over the Pacific Northwest is modest and tied largely to ENSO. The
  long-lead forecast is mostly a question of a good starting state, which is
  why ESP is the standard for water-supply forecasts (Day 1985).

## Two ways to combine forecasts with the inflow model

**A. Forcing.** Train the inflow model on the 30-year historical table, then run
it with each corrected forecast member. One model serves every system and lead;
it learns from 30 years rather than from the shorter hindcasts; and a new
forecast system needs only a new correction. Forecast errors pass straight into
the inflow, so the correction and the ensemble structure have to be good.

**B. Trained on forecasts.** Give the model the forecast itself as input during
training, for example a hindcast/forecast LSTM that reads observed forcing up to
the start date and forecast forcing after it (Nearing et al. 2024). The model
learns the forecast's systematic errors, but it needs years of hindcasts for
each system and lead, has to be retrained when the system changes, and overfits
a short archive (WeatherNext 2 has four years, the ECCC short-range archive
nine, with model changes).

Start with A. Try B for GEFS v12 (26 years, one model version), the ECMWF
reforecasts (20 years per cycle) and C3S, and keep it only where it beats A on
the hindcasts.

## Correcting the forecasts

- The target is the historical table. Fit per catchment, calendar month and lead
  day, pooling the neighbouring weeks of the year and neighbouring leads so that
  each fit has enough data.
- Temperature and wind: quantile mapping. Precipitation: quantile mapping with
  a separate treatment of dry days, or a censored, shifted gamma fitted to the
  ensemble (Scheuerer and Hamill 2015).
- Correcting each day and catchment on its own scrambles how members evolve in
  time and how catchments vary together. Reorder the corrected members with the
  Schaake shuffle (Clark et al. 2004) using sequences from the historical table,
  as the NWS Hydrologic Ensemble Forecast Service does (Demargne et al. 2014).
- Compute rainfall, snowfall, relative humidity, VPD and PET after the
  correction, from the corrected precipitation and temperature, with the
  formulas of the historical table.
- Fit the correction with one year left out at a time, so that hindcast skill
  is not flattered.
- Refit when a system changes: ECMWF reruns its reforecasts with each model
  cycle, GEPS runs them with the operational model, and GEFS v12 is frozen.

## Lead ranges

- **Days 1-15: multi-model daily ensemble.** ECMWF IFS ENS, GEFS v12 and GEPS 8,
  each corrected, pooled with equal weights at first and with weights from
  hindcast CRPS once those are known. In the Canadian catchments, HRDPS and
  RDPS can be tested for days 1-2, corrected on their 2017-2025 archive.
- **Days 16-46: weekly.** ECMWF extended range (to 46 days), GEFS v12
  (Wednesdays, to 35 days) and GEPS 8 (to 39 days), corrected and verified as
  weekly totals and means; daily values are only a way to feed the model. Past
  the end of each system, the members continue as ESP traces.
- **Days 60-180: ESP, conditioned on the seasonal forecasts.** Compare three
  versions on the hindcasts:
  1. plain ESP: every historical year's weather from the table, run from the
     current snow and soil;
  2. ESP with the years weighted by how well their monthly temperature and
     precipitation match the C3S multi-model forecast for the season;
  3. corrected C3S daily forcing, member by member.

  Use 2 or 3 only where it beats 1. My expectation is that 2 helps mostly
  through temperature, and that 3 needs the most correction for the least gain.
- **Across ranges:** use each system up to the lead where it stops beating ESP
  in the hindcasts, then continue with ESP, so one inflow forecast runs from
  day 1 to day 180.

## Inputs

- The core inputs are precipitation, Tmean, Tmax, Tmin, PET (from temperature)
  and wind. Every chosen system supplies them; for 6-hourly systems, Tmax and
  Tmin in training are also taken from 00, 06, 12 and 18 UTC values only.
- Humidity, radiation and cloud cover are added only if the comparison on the
  historical table shows they help, and only for systems that supply them.
- SWE and soil moisture enter at the start date, from the historical table in
  hindcasts and from the real-time substitute in operations. Forecast snow, soil,
  snowmelt and AET from the weather models are not used: their land models
  differ from ERA5-Land and from each other.

## Verification

- On the hindcasts, by catchment, month and lead: CRPS skill against ESP and
  climatology, bias, spread against error, and rank histograms.
- Both the catchment weather (precipitation and temperature against the
  historical table) and the inflow (against observed or naturalized inflow, not
  regulated releases).
- For leads beyond 60 days: error of the seasonal volume (for example April to
  July) and whether the forecast tercile probabilities are reliable.

## Real time

- AORC is not available in real time and ERA5-Land arrives about five days late.
  The start-date state comes from ERA5T or the IFS analysis, and the last days
  of precipitation from MRMS / Stage IV in the United States and CaPA in Canada.
  Each substitute is compared with the historical table on the years they
  overlap, and corrected the same way as the forecasts.
- Runs: daily at 00 UTC for ECMWF, GEFS and GEPS; monthly when the seasonal
  forecasts are released.
- Keep every forecast received. The MSC Datamart keeps only about 30 days, and
  the archive becomes the hindcast for the next correction.
- Watch for model upgrades and refit the corrections when one happens.

## Order of work

1. ESP inflow hindcasts with the model trained on the historical table. This
   needs no forecast data and is the baseline everything else must beat.
2. GEFS v12, the ECMWF S2S reforecasts and the GEPS 8 reforecast for days 1-46,
   corrected and shuffled; inflow hindcasts and skill against ESP.
3. C3S seasonal forecasts for days 60-180: conditioned ESP and corrected daily
   forcing, against plain ESP.
4. Forecast-trained models (B) where hindcasts allow; WeatherNext 2, AIFS and
   the ECCC short-range models once their records are long enough.

## References

- Clark, M. et al. (2004). The Schaake shuffle: a method for reconstructing
  space-time variability in forecasted precipitation and temperature fields.
  *Journal of Hydrometeorology* 5, 243-262.
- Day, G. N. (1985). Extended streamflow forecasting using NWSRFS. *Journal of
  Water Resources Planning and Management* 111, 157-170.
- Demargne, J. et al. (2014). The science of NOAA's operational Hydrologic
  Ensemble Forecast Service. *Bulletin of the American Meteorological Society*
  95, 79-98.
- Nearing, G. et al. (2024). Global prediction of extreme floods in ungauged
  watersheds. *Nature* 627, 559-563.
- Scheuerer, M. and Hamill, T. M. (2015). Statistical postprocessing of ensemble
  precipitation forecasts by fitting censored, shifted gamma distributions.
  *Monthly Weather Review* 143, 4578-4596.
