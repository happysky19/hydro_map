# Forecast hindcasts for the inflow models

Checked 2026-10-12. The aim is a forecast table in the same form as the
historical catchment table (same polygons, UTC days, column names, units and
formulas) for leads of 1 to 180 days, with hindcasts long enough to bias-correct
each system per catchment, calendar month and lead, to measure its skill, and to
train or test inflow models on forecast inputs. Hindcasts are useful only where
they overlap the historical table, 1996-2025: AORC for 2026 has not been
released. How the forecasts would be used is in
[using forecast data](forecast_use.md).

## Systems by lead time

| Lead | Systems | Hindcasts within 1996-2025 |
| --- | --- | --- |
| 1-3 days | ECCC HRDPS, RDPS, REPS | None; real-time archive from 2015 (RDPS) or 2017 |
| 1-15 days | GEFS v12, ECMWF IFS ENS, ECMWF AIFS, WeatherNext 2, ECCC GEPS 8, ECCC GDPS (10 days) | GEFS 2000-2025; ECMWF reforecasts over the 20 years before each start; IFS open-data archive from 2024-03-06; WeatherNext 2 2022-2025; GEPS 2001-2020; GDPS archive from 2017; AIFS none |
| 16-46 days | ECMWF extended range, GEFS v12 (Wednesday starts, to 35 days), GEPS 8 (Monday and Thursday starts, to 39 days) | ECMWF reforecasts; GEFS 2000-2019 and operational GEFS v12 from 2020-09; GEPS 2001-2020 |
| 60, 120, 180 days | C3S seasonal multi-system, including ECCC CanSIPS | 1993-2016 for every system, longer for some (table below), plus later real-time forecasts of the same system |

No sub-seasonal or machine-learned system reaches 60 days, so leads of 60, 120
and 180 days come from seasonal systems. These start once or a few times a month,
so a lead of 60 days is sampled from those start dates only.

## Hindcast sources

| Source | Overlap with 1996-2025 | Starts and members | Grid and step | Access | Volume |
| --- | --- | --- | --- | --- | --- |
| [GEFS v12 reforecast](https://registry.opendata.aws/noaa-gefs-reforecast) | 2000-2019; operational GEFS v12 (same model) from 2020-09-23 | Every day at 00 UTC, 5 members to 16 days; Wednesdays 11 members to 35 days | 0.25°, 3-hourly to day 10; 0.5°, 6-hourly after | AWS `noaa-gefs-retrospective`, no account | Global GRIB2 file per variable: about 0.55 GB per member and start for the core variables to day 10, 2.2 GB with pressure levels for the freezing level. Must be sampled and processed as a stream |
| [ECMWF reforecasts in the S2S database](https://ecds.ecmwf.int/datasets/s2s-reforecasts) | The 20 years before each start date; reforecasts made with IFS 49r1 (2024-11-12 to 2026-05-12) cover about 2004-2025 | 11 members per start, to 46 days | 1.5°; daily, 6- or 24-hourly by field; pressure levels for the control member only | ECMWF data portal, free for research and education | Small (area subset on the server) |
| IFS ENS open-data archive | 2024-03-06 to 2025-12-31 with most fields. Before that only 2t, 10 m wind, tp, msl, sp, skt, soil temperature and tcwv; SWE, snowfall and cloud cover from 2025-11-21 | 51 members, 00 and 12 UTC, to 15 days | 0.25°, 3-hourly to 144 h, 6-hourly after | AWS `ecmwf-forecasts` (byte ranges from the `.index` files); [dynamical.org](https://dynamical.org/catalog/) has a Zarr copy with fewer variables | Moderate; real-time forecasts, IFS 48r1 then 49r1 |
| ECMWF MARS | Full-resolution reforecasts with every parameter (smlt, e, deg0l) | As the S2S reforecasts | Native | ECMWF account and research licence; the information cost was removed on 2025-10-01 but service charges may apply ([distribution rules](https://www.ecmwf.int/sites/default/files/2023-07/ECMWF_Product_Distribution_Rules.pdf)) | Ask ECMWF |
| [WeatherNext 2](https://developers.google.com/weathernext/guides/models-wn2) archive | 2022-2025. Google does not state the training period; confirm 2022 is outside it | 64 members, 4 starts a day, to 15 days | 0.25°, 6-hourly | BigQuery, Earth Engine; GCS Zarr after a data request | Small with server-side subsetting |
| AIFS Single / ENS v2 | No public hindcast. The real-time archive starts in 2024 and changed version three times; v2 starts 2026-05-12, after AORC | Self-run from ERA5 initial conditions with the [open weights](https://huggingface.co/ecmwf/aifs-single-2.0) | 0.25°, 6-hourly | anemoi-inference, GPU | GPU hours. v2 was trained on ERA5 1979-2022 and fine-tuned on 2018-2024 analyses, so only 2025 is out of sample |
| GEPS 8 reforecast | 2001-2020 | 3+1 members, Monday and Thursday at 00 UTC, to 39 days, run with the current model ([S2S ECCC](https://confluence.ecmwf.int/display/S2S/ECCC+Model)) | 1.5° in the S2S database; 1° in SubX | ECMWF data portal; IRI Data Library (login) | Small |
| [CaSPAr](https://caspar-data.ca) archive of ECCC forecasts | GDPS and HRDPS from 2017-05, REPS from 2017-06, RDPS from 2015-01, GEPS (North America) from 2021-05 | As run operationally; several model upgrades in the period | Native grids, not interpolated | Free account and a Globus endpoint; subsets by area, variable and date | Small after cropping |
| [C3S seasonal](https://cds.climate.copernicus.eu/datasets/seasonal-original-single-levels) | See below | See below | 1°, 6- or 24-hourly | CDS, existing client | Small |

C3S seasonal systems ([status 2026-04](https://confluence.ecmwf.int/display/CKB/Description+of+the+C3S+seasonal+multi-system)):

| System | Length | Hindcast years | Hindcast members | Starts |
| --- | --- | --- | --- | --- |
| ECMWF SEAS5 | 215 days | 1981-2016 | 25 | 1st of the month |
| Météo-France System 9 | 7 months | 1993-2024 | 15 | Monthly |
| DWD GCFS2.2 | 6 months | 1993-2023 | 30 | 1st of the month |
| CMCC SPS4 | 184 days | 1993-2022 | 30 | 1st of the month |
| JMA CPS4 | 215 days | 1993-2020 | 5 | Two lagged starts a month |
| NCEP CFSv2 | 215 days | 1993-2016 | 4 | Every 5 days |
| Met Office GloSea6 | 215 days | 1993-2016 | 7 | 1st, 9th, 17th, 25th |
| ECCC CanESM5.1 / GEM5.2-NEMO | 214 days | 1993-2023 | 10 | End of the previous month |
| BOM ACCESS-S2 | 217 days | 1993-2018 | 3 | 1st and end of the previous month |

## Variables by system

✓ the same quantity can be computed with the historical formula; ≈ a different
layer, aggregation or a derivation (recompute the training column the same way,
or bias-correct); – not available. Every forecast value still differs from the
historical one in grid resolution and model climate, so every column is
bias-corrected.

| Columns | GEFS v12 | IFS ENS open data | ECMWF S2S | AIFS v2 | WeatherNext 2 | GEPS 8 | C3S seasonal |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `precipitation_mm` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `rainfall_mm`, `snowfall_mm` | ✓ T, q, p 3-hourly | ✓ 2t, 2d, sp | ≈ daily-mean 2t, 2d | ✓ 6-hourly 2t, 2d, sp | ≈ q from 1000/925 hPa, or a temperature-only rule | ✓ T, q, p 3-hourly | ≈ 6-hourly 2t, 2d, daily tp |
| `tmean_degC` | ✓ | ✓ | ✓ | ✓ 4 samples | ✓ 4 samples | ✓ | ✓ 4 samples |
| `tmax_degC`, `tmin_degC` | ✓ | ✓ mx2t3/mn2t3 | ✓ mx2t6/mn2t6 | ≈ 4 samples | ≈ 4 samples | ✓ TMAX/TMIN | ≈ grid-point 24 h extremes |
| `snow_water_equivalent_mm` | ✓ WEASD | – (from 2025-11-21) | ✓ sd | – | – | ✓ WEASD | ✓ |
| `snow_depth_m` | ≈ SWE / density climatology | – | ✓ sd, rsn | – | – | ✓ SNOD | ✓ |
| `snowmelt_mm` | ≈ snowfall - ΔSWE | – | ≈ sf - Δsd | – | – | ≈ snowfall - ΔSWE | ≈ snowfall - ΔSWE |
| `freezing_level_*_m` | ≈ pressure levels | ≈ pressure levels | ≈ control member only | ≈ pressure levels | ≈ pressure levels | ≈ pressure levels | ≈ pressure-level dataset |
| `soil_moisture_layer1_m3_m3` | ≈ 0-10 cm | ✓ | ≈ 0-20 cm | ✓ | – | ≈ 0-10 cm | ≈ one layer, check per system |
| `root_zone_soil_moisture_0_100cm_m3_m3` | ≈ Noah 0-1 m | ✓ | ✓ sm100 | – (0-28 cm only) | – | – | – |
| `soil_moisture_layer4_m3_m3` | ≈ 1-2 m | ✓ | – | – | – | – | – |
| `soil_temperature_layer1_degC` to `layer4_degC` | ≈ Noah layers | ✓ | ≈ 0-20, 0-100 cm | layers 1-2 ✓, 3-4 – | – | ≈ 0-10 cm only | layer 1 only |
| `relative_humidity_pct`, `vapor_pressure_deficit_kPa` | ✓ | ✓ | ≈ daily means | ✓ | ≈ q from pressure levels | ✓ | ✓ |
| `wind_speed_m_s`, `u_wind_m_s`, `v_wind_m_s` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `shortwave_down_mean_W_m2` | ✓ | ✓ | ✓ | ✓ | – | ✓ | ✓ |
| `net_radiation_mean_W_m2` | ✓ four components | ✓ | ✓ | ≈ ssrd, strd, skt and an albedo climatology | – | ✓ four components | ✓ |
| `pet_hargreaves_mm_day` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `actual_evapotranspiration_mm` | ✓ LHTFL | – | ✓ slhf | – | – | ✓ LHTFL | ✓ |
| `cloud_cover_fraction` | ✓ | – (from 2025-11-21) | ✓ | ✓ | – | ✓ | ✓ |

The GEPS 8 column is its real-time 0.5° output on the MSC Datamart; its
reforecast in the S2S database has the smaller S2S field set, to be checked
field by field. GDPS, RDPS, HRDPS and REPS are GEM models with output similar to
GEPS. [WeatherNext 3](https://developers.google.com/weathernext/guides/models) adds
2 m dewpoint, solar radiation and cloud cover at 0.1° and hourly steps, but its
history starts in 2024, so it is left for later.

## Canadian systems

Nine of the 43 catchments are in Canada (Mica to Waneta, 65,000 km2), and US
catchments such as Libby have Canadian headwaters.

| System | Model | Grid | Lead and starts | Hindcast or archive |
| --- | --- | --- | --- | --- |
| HRDPS | GEM | 2.5 km | 48 h, 4 a day | Archive from 2017 (CaSPAr) |
| RDPS | GEM | 10 km | 84 h, 4 a day | Archive from 2015 (CaSPAr) |
| REPS | GEM, 20 members | 10 km | 72 h | Archive from 2017 (CaSPAr) |
| GDPS | GEM coupled to NEMO-CICE; since May 2026 nudged toward GEML | 15 km (0.15° output) | 10 days, 00 and 12 UTC | Archive from 2017 (CaSPAr); the hybrid version has no data before 2026 |
| GEPS 8 | GEM, 20+1 members | 0.25° (0.5° output) | 16 days at 00 and 12 UTC; 39 days on Monday and Thursday | Reforecast 2001-2020, 4 members; archive from 2021 (CaSPAr, North America) |
| GEML | Machine-learned emulator (GraphCast type) | 0.25° | 10 days | None. Its Datamart output has no precipitation or 2 m humidity, so it is not used on its own |
| CanSIPS | CanESM5.1 and GEM5.2-NEMO | 1° | 7 months (12 on the Datamart) | Daily hindcasts 1993-2023 in C3S; monthly means 1990-2020 on the Datamart |

- HRDPS, RDPS, REPS and GDPS have no reforecasts. Their archives give
  2017-2025, several model versions included; at leads of 1-3 days the bias is
  expected to come mostly from terrain and resolution and to change less between
  versions. Version changes are checked with the same step test used on the
  historical table.
- GEPS 8 and GEFS v12 both have reforecasts covering 2001-2019, so a combined
  GEFS + GEPS ensemble (the idea behind NAEFS) can be calibrated on the same
  period.
- AORC is weaker in Canada (NLDAS-2 there, sparse gauges). ECCC's
  [CaSR v3.2](https://hpfx.collab.science.gc.ca/~scar700/rcas-casr/download.html)
  surface reanalysis (1980-2024, 10 km, hourly; precipitation from CaPA, ECCC's
  gauge-based precipitation analysis; SWE and snow depth included) is a
  Canadian reference to compare with AORC and ERA5-Land, and is from the same
  model family as the ECCC forecasts. In real time, CaPA (RDPA 10 km, HRDPA
  2.5 km) and CaLDAS give recent precipitation and land states.
- The MSC Datamart keeps about 30 days. Anything not archived in CaSPAr has to
  be pulled daily from now on.

## Missing variables

- A model run with system X uses only the columns X supplies (✓ or ≈). The
  training column is computed the same way: for a 6-hourly system, Tmax and Tmin
  are the extremes of the four 00/06/12/18 UTC values; for WeatherNext 2,
  humidity comes from the same pressure levels in ERA5; the freezing level is
  derived from the same pressure levels in ERA5 for every system.
- Weather forcing (precipitation, temperature, humidity, wind, radiation, cloud,
  PET, freezing level) is needed over the forecast. Precipitation and
  temperature carry most of the inflow signal and are available everywhere; the
  rest can be dropped where missing.
- Snow and soil states (SWE, snow depth, soil moisture and temperature) are
  needed at the start date, not over the forecast: take them from the historical
  table (in real time from ERA5-Land or the IFS analysis) and let the inflow
  model carry them forward with forecast precipitation and temperature. If a
  model needs them over the forecast, keep the deep soil at its start value and
  run a degree-day snow model calibrated on ERA5-Land SWE.
- Snowmelt and AET are land-model fluxes; leave them out as forecast inputs.
- Whether dropping is acceptable is tested in step 1 below.

## Work plan

1. **Input sets from the historical table.** Train the inflow model on the full
   set and on reduced sets (precipitation, temperature, PET and start-date
   states; plus each system's available columns) and compare KGE on observed
   inflow. This needs no forecast data and fixes the columns to extract from
   each system.
2. **Forecast table and aggregation.** One Parquet table per system:
   `init_date`, `lead_day`, `member`, `project_id`, `valid_date` and the
   historical column names. Lead day 1 is the UTC day starting at a 00 UTC
   start; accumulations are differenced between steps; the same polygons and
   fractional weights are used, which also works where a catchment is smaller
   than one grid cell.
3. **Seasonal, 60/120/180 days.** C3S through the existing CDS client: SEAS5,
   Météo-France 9, DWD, CMCC, CFSv2 and the two CanSIPS models first; daily
   single-level fields and
   pressure-level t and z for the freezing level; hindcasts and the real-time
   forecasts of the same system up to 2025.
4. **ECMWF S2S reforecasts, 1-46 days.** 49r1 reforecasts for every start in a
   year, all 11 members.
5. **GEFS v12 reforecast, 1-35 days, on the HPC.** Download, average and delete
   each file as it arrives. Start with the Wednesday starts (2000-2019) and five
   members, about 5 TB in total; add members and starts as throughput allows.
   Continue with the operational GEFS v12 archive for 2020-2025.
6. **ECCC.** Add CaSR v3.2 to the historical table and compare it with AORC
   in the Canadian catchments; extract the GEPS 8 reforecast (2001-2020, to 39
   days); request CaSPAr subsets of HRDPS, RDPS, REPS, GDPS and GEPS for
   2017-2025 cropped to the basin. This needs a Globus endpoint where the data
   are processed.
7. **WeatherNext 2, 1-15 days.** Request access and extract 2022-2025.
8. **ECMWF at 0.25°.** The open-data archive 2024-03-06 to 2025-12-31 shows what
   the 1.5° S2S grid loses; request a MARS research licence in parallel.
9. **AIFS.** Wait for AORC 2026 or an official AIFS reforecast; with a GPU,
   run v2 from ERA5 for weekly starts in 2025 as a test.
10. **Bias correction and skill.** Quantile mapping per catchment, calendar month
    and lead (pooled over nearby leads), fitted on the hindcasts against the
    historical table; CRPS, bias and spread by lead; weekly and monthly totals
    for leads beyond 30 days.

In real time AORC is not available and ERA5-Land arrives about five days late,
so start-date states and the most recent days need a substitute (ERA5T, the IFS
analysis, MRMS for precipitation in the United States, CaPA and CaLDAS in Canada)
whose difference from the historical table is checked on the overlap.
