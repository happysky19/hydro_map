# Daily catchment forecast hindcasts

Daily catchment means of three ensemble weather forecast systems for the 46 dam catchments of the
history table, completed into every column of that table, for 285 forecast starts from 2023-01-01
to 2025-12-29. The tables are made to be used with `catchment_daily_46_1996_2026.parquet`: a
model trained on the history table can read each ensemble member as the history continued.

## Files

One Parquet file per start, named `YYYYMMDD00.parquet` after the start time (00 UTC), and
`check.csv` with scores against the history table. Read a start with
`pandas.read_parquet('2024070100.parquet')`. It has six key columns, then the 28 value columns
of the history table with the same names, units and definitions.

| Column | Meaning |
| --- | --- |
| `init_time` | Start of the forecast, 00 UTC (timestamp) |
| `system` | `aifs` (AIFS-ENS v2, ECMWF), `wn2` (WeatherNext 2, Google DeepMind) or `gefs` (GEFS v12, NOAA) |
| `member` | Ensemble member: `p00` to `p20` for aifs; `ckpt1_s000`, `ckpt2_s003`, ... for wn2; `c00` and `p01` to `p10` for gefs |
| `lead_day` | -4 to 22; lead day 1 is the 24 hours after the start, 00 to 24 UTC of the start date |
| `valid_date` | UTC calendar day of the row: the start date plus `lead_day` minus 1 |
| `project_id` | Catchment, as in the history table |

- Each member is one series from lead day -4 to lead day 22: 27 days x 46 catchments = 1,242 rows.
  A start with all three systems complete has 21 + 21 + 11 = 53 members, 65,826 rows.
- Lead days 1 to 22 are the member's own forecast. Lead days -4 to 0, the five days before the
  start, are the same for every member of a start: they bridge the gap between the last day of the
  history table (lead day -5, six days before the start, which is how late the history runs in
  operations) and the start, and hold the day-ahead forecast of GEFS for each of those days.
- Starts are 00 UTC on days 1, 5, 9, 13, 17, 21, 25 and 29 of every month (8 a month, 7 in
  February). Four starts have no aifs rows (2023-01-01, 2023-01-05, 2024-07-13, 2025-10-01) and
  2024-10-01 has one aifs member; wn2 and gefs are complete on every start.
- The three systems are independent ensembles of 21, 21 and 11 members. There is no pairing
  between members of different systems, and the GEFS control `c00` is used as one member like the
  others.

| System | Producer | Members | Steps | Grid |
| --- | --- | --- | --- | --- |
| AIFS-ENS v2 | ECMWF | 21 | 6 h to 528 h | Reduced Gaussian N320 (about 0.28°) |
| WeatherNext 2 | Google DeepMind | 21 (4 checkpoints x seeds) | 6 h to 528 h | 0.25° |
| GEFS v12 | NOAA | 11 (control and 10 perturbed) | 6 h to 528 h | 0.5° |

The day-ahead forecast of GEFS also fills lead days -4 to 0 of every system.

## How the values are made

1. **Aggregation.** Each member's fields are averaged over each catchment with the area weights of
   the history table, on UTC days. Precipitation is the 24-hour amount; temperatures, wind and
   humidity use the 6-hourly samples of the day.
2. **Adjustment.** Each forecast weather column is quantile-mapped onto the history table of the
   same dates, per catchment, lead-day group (1, 2, 3, 4-5, 6-7, 8-10, 11-14, 15 on) and
   calendar month with the months either side; the fit pools the members and each member is mapped
   on its own. Each year's starts are mapped with a fit on the other two years, so a forecast never
   sees its own year. Precipitation is split into rainfall and snowfall by the catchment's
   snow-fraction curve from the history at the mapped mean temperature; tmin, tmean and tmax are
   put in order and PET is recalculated from them.
3. **Completion.** The three systems give only the weather. The other columns are made for each
   member from its own adjusted weather, as a hydrological model would, starting from the history
   table's state on the anchor day (lead day -5). The completion is fitted on the history before
   2023, so the hindcasts never see their own years.

| Columns | How |
| --- | --- |
| Precipitation, rainfall, snowfall, tmean, tmax, tmin, u and v wind, wind speed, PET; relative humidity, vapor pressure deficit and shortwave for aifs and gefs | The member's own forecast, adjusted |
| Snow water equivalent, snowmelt | A degree-day snowpack from the history SWE on the anchor day, driven by the member's snowfall and mean temperature; melt factor and threshold fitted per catchment on the history |
| Relative humidity, vapor pressure deficit and shortwave for wn2; net radiation, cloud cover, actual evapotranspiration, the two freezing levels, soil temperature layer 1, snow depth | A linear regression per catchment and calendar month (with its neighbours) on the member's weather and SWE, fitted on the history |
| Soil moisture (three columns), soil temperature layers 2 to 4 | The history value on the anchor day plus the change of the day-of-year climatology; these do not respond to the member's weather |

## Checks

`check.csv` compares the forecasts with the history table on the same days: per system, column and
lead day, the number of cases, the history mean, the bias and mean absolute error of the ensemble
mean, and the CRPS of the ensemble. Precipitation in mm/day, all catchments and starts pooled:

| System | Lead day | Bias | CRPS |
| --- | --- | --- | --- |
| aifs | 1 | -0.019 | 0.480 |
| aifs | 5 | 0.047 | 1.056 |
| aifs | 10 | 0.014 | 1.680 |
| aifs | 15 | 0.149 | 1.818 |
| wn2 | 1 | -0.028 | 0.420 |
| wn2 | 5 | 0.038 | 0.889 |
| wn2 | 10 | 0.023 | 1.625 |
| wn2 | 15 | 0.103 | 1.808 |
| gefs | 1 | 0.016 | 0.707 |
| gefs | 5 | 0.017 | 1.283 |
| gefs | 10 | -0.017 | 1.851 |
| gefs | 15 | 0.148 | 1.906 |

GEFS has the highest CRPS at every lead day, about 50% above aifs on day 1 and 5% above on
day 15; part of the difference comes from its 11 members against 21. Over July-September
2023-2025 the three systems' lead day 1-5 precipitation is within 5% of the history at Hungry
Horse and Kerr, but GEFS varies far more from year to year (at Hungry Horse 33% below the history
in 2024 and 21% above in 2025).

Fed the history's own weather over 2023-2025, the snow model's SWE error is 12.0 mm, against
21.4 mm for carrying the anchor SWE forward with its climatology.

## Limitations

- AIFS-ENS v2 was trained on data to the end of 2024, so its 2023 and 2024 hindcasts lie within
  its training period and 2025 is the first year outside it. The training period of WeatherNext 2
  is not published. Hindcast skill may therefore be somewhat better than operational forecasts
  will show.
- From 2016 every weather column of the history table in the US catchments is ERA5-Land mapped
  onto AORC of 2006-2015, and on every day in the twelve catchments with a large part in British
  Columbia (see the history README). Over 2023-2025 the forecasts are therefore mapped onto, and scored against, values
  from the reanalysis family the AI models were trained on, which makes their agreement look
  better than it will against independent observations.
- Soil moisture and soil temperature layers 2 to 4 carry no forecast information beyond the
  anchor day.
- Lead days -4 to 0 hold one set of values shared by every member; ensemble spread begins at lead
  day 1.
- Operational forecasts will be made by the same code with the same mappings and a completion
  fitted on every history year; the number of members does not matter (AIFS runs 51 members
  operationally; GEFS is taken with the same 11 members).
