# Historical forcing source assessment

Checked 6 October 2026. Research status: **AORC is the leading candidate; no source has passed full-period, full-catchment validation.** These scripts are separate from the production CLI.

## Decision

Start with AORC 1.1 for the core meteorological forcing over both US and Canadian catchments. Compare precipitation and temperature with Daymet V4 R1. Evaluate ERA5-Land as a consistent alternative forcing experiment and a source of supplementary land states; use ERA5 for atmospheric fields unavailable in the other products. Do not splice sources at the international border without evaluating the resulting discontinuity.

The working period is 1996–2025, with 10,958 Gregorian days and 284,908 project-day rows for 26 projects. This is a target, not a completeness claim. The currently inspected AORC Zarr archive lacks the final precipitation boundary hour for this UTC window. A 1995–2024 window avoids that specific endpoint issue; it still needs all other checks below. Final day boundaries must match the selected flow target.

| Source | Proposed use | Evidence obtained | Outstanding issue |
| --- | --- | --- | --- |
| NOAA AORC 1.1, hourly, 30 arc seconds | Core forcing; preserve hourly inputs before daily aggregation | Actual temperature and precipitation chunks decoded at US and Canadian locations in 1996 and 2025 | Zarr masking correction, all-area/all-time checks, final boundary hour, other six variables |
| NASA/ORNL Daymet V4 R1, daily, 1 km | Temperature and precipitation comparison across the border | Catalog plus authenticated native precipitation samples near both dams in 1996 and 2025 | Public point requests timed out; polygon extraction and other variables not tested; leap-year calendar |
| Copernicus ERA5-Land, hourly, native about 9 km | Alternative experiment; snow and soil estimates | Official catalog and processing documentation | Authenticated sample retrieval and local validation not performed |
| Copernicus ERA5, hourly | Cloud cover and vertical temperature profiles | Official single-level and pressure-level products identified | Same access and validation gates; coarser terrain representation |
| USGS / ECCC HYDAT / reservoir operators | Streamflow, reservoir inflow and regulation targets | Three approved historical USGS daily-flow records retrieved | A separate station/series mapping for every project; Canadian and operator access untested |
| NRCS SNOTEL / provincial snow observations | Snow evaluation at station locations | Official observing programs identified | Station inventory, representativeness, gaps and measurement history |

An official source can still contain interpolation error, model bias or missing records. Successful HTTP access establishes availability of that response, not hydrologic skill.

## What transfers from NWRFC

The [2026 NWRFC calibration paper](https://repository.library.noaa.gov/view/noaa/73306/noaa_73306_DS1.pdf), especially Sections 2–3, describes AORC precipitation, temperature, specific humidity and surface pressure. Wet-bulb temperature supports rain/snow partitioning; zone temperature supports Hargreaves–Samani PET. SNOW-17, SAC-SMA and routing represent snow, soil and flow processes. The case studies use six-hour integration and daily calibration objectives. This supports retaining hourly meteorology even when the final requested table is daily. It does not establish that every current operational input is AORC or that an arbitrary HydroBASINS polygon reproduces an NWRFC zone.

The [NWRFC calibration repository](https://github.com/NOAA-NWRFC/nwsrfs-hydro-autocalibration) expects zone MAP, MAT and PTPS, and separate upstream flow for routing. Its time-step conventions also differ between forcing and simulated flow. Therefore local precipitation cannot, on its own, explain a downstream dam's release. Upstream flow, storage changes, diversions and the target's meaning must be explicit.

NWRFC publishes [operational forcing and model-output downloads](https://www.nwrfc.noaa.gov/misc/downloads/index.php?filter=&sortasc=true&sortby=date&type=forecast_precipitation_points). No complete 30-year operational forcing archive for these 26 geometries was verified here. NOAA also maintains an [AORC NWRFC regional NetCDF archive](https://hydrology.nws.noaa.gov/pub/AORC/V1.1/NWRFC_1km/), packaged as monthly TAR files. That listing reaches December 2025. It is a potential reference for checking the Zarr conversion; its multi-gigabyte monthly files were not downloaded in this assessment.

## Access tests and limitations

### AORC

Anonymous requests to `noaa-nws-aorc-v1-1-1km` succeeded. The root listed 1979–2025. Sampled coordinate arrays span 20–54.9986°N and 130–60.0028°W, including masked padding. Use those coordinate arrays and masks, not an approximate catalog extent, to locate cells. Expected padding outside the data domain is distinct from the announced masking defect.

Twelve meteorological chunks were decoded: two variables, three locations, two years. Each point had 144 valid hourly temperature and precipitation values for January 1–6. These are grid-cell access checks, not catchment-average results.

| Sample cell | Longitude | Latitude | 1996 Jan 1 mean T / total P | 2025 Jan 1 mean T / total P |
| --- | ---: | ---: | ---: | ---: |
| Mica northern tip | −118.992107 | 52.865352 | −12.246°C / 1.3 mm | −15.992°C / 3.0 mm |
| Mica interior | −117.183846 | 51.498740 | −10.254°C / 0.0 mm | −4.092°C / 0.7 mm |
| Brownlee interior | −114.000640 | 43.174073 | −1.971°C / 0.0 mm | −8.183°C / 1.6 mm |

All three centers fall inside their catchments. A fractional cell-intersection check found that the sampled northern chunk covers 26.7285% of Mica's area, with all of that intersected area valid for both variables throughout the sampled hours in both years. This does not establish complete Mica coverage or areal coverage for the other 25 projects; Brownlee received only a point check. Area fractions in this diagnostic used EPSG:6933; production polygon areas use the WGS84 ellipsoid.

The [official catalog](https://registry.opendata.aws/noaa-nws-aorc/) still carries an April 2, 2026 notice about incorrectly masked rows, estimated at 0.04%, with corrected Zarr files being regenerated. Do not infer local impact from this global percentage. Before bulk use, establish correction status and check every intersecting cell and required timestamp; compare suspicious values with native NetCDF.

AORC precipitation is an hourly amount ending at its timestamp. UTC January 1 requires hours ending January 1 01Z through January 2 00Z. The inspected 2025 timestamps end at December 31 23Z, so December 31's last hour is absent from this bucket. Never silently total 23 hours as a complete day. Temperatures are instantaneous: the mean of 00–23Z samples approximates a daily mean, and sampled maxima/minima are not continuous-time extrema.

The [NOAA methods report](https://hydrology.nws.noaa.gov/pub/AORC/V1.1/Documents/AORC-Version1.1-SourcesMethodsandVerifications.pdf) includes Canadian contributing watersheds and Columbia climatology processing. Coverage therefore need not stop at the border, but method and observation-density changes still need evaluation. Include the 2015–2016 transition in auxiliary meteorological fields in consistency tests.

### Daymet

The [current NASA guide](https://data.ornldaac.earthdata.nasa.gov/public/daymet/Daymet_Daily_V4R1/comp/Daymet_Daily_V4R1.pdf), revised May 22, 2026, identifies the 2025 release. Live CMR queries returned all seven named North American annual variables for 1996 and 2025, and none for 2026. V4 R1 repaired Canadian January input gaps in 2020–2021. Canadian station inputs changed in 2024–2025 because of feed problems; assess temporal consistency around those years.

Two public single-pixel requests, Mica/1996 and Brownlee/2025, each timed out after 55 seconds with zero payload bytes. An unauthenticated OPeNDAP metadata request required Earthdata Login and returned HTTP 401. These failures do not establish that the data are absent. Following the [current OPeNDAP tutorial](https://github.com/ornldaac/daymet/blob/main/tutorials/daymet_subset_opendap.ipynb), authenticated access then succeeded: metadata (7,044 bytes), native coordinate axes (24,888 bytes), and two precipitation subsets (44,541 and 44,596 bytes), all HTTP 200.

The subsets cover February 28–March 1, 1996 and December 29–31, 2025. Native centers near Mica (−118.564621, 52.077110) and Brownlee (−116.895027, 44.844917) are inside their respective local polygons. Both have valid precipitation values on all six dates (0.0 mm/day at the selected centers; fill is −9999). Decoded 1996 day-of-year values 59, 60 and 61 confirm February 29 is retained. The files explicitly label local 24-hour days; their noon CF time coordinates must not be interpreted as UTC event times. This validates authenticated sample-value retrieval, not polygon extraction, temperature access or full-period coverage. These locations also differ from the AORC interior samples; no direct numeric cross-source comparison is claimed. AppEEARS polygon access remains untested.

Daymet has 365 records every year, including February 29 and omitting December 31 in leap years. The target window therefore has 10,950 Daymet records and eight missing Gregorian dates. Retain those dates with missing-value flags in a joined table; do not shift the remaining days or quietly interpolate precipitation. Its `srad` is a daylight mean: multiply by `dayl` for daily energy, or by `dayl/86400` for a 24-hour mean. [Calendar and variable definitions](https://daymet.ornl.gov/overview)

Daymet SWE is a simple model estimate, not independent snow sensing. The [methods paper](https://pmc.ncbi.nlm.nih.gov/articles/PMC8302764/) cautions against treating it as a general-purpose snowpack model. The daily weather convention targets local days, which requires reconciliation with UTC forcing and station flow days before event-scale comparisons.

### Observed discharge

A request to the [USGS daily OGC API](https://api.waterdata.usgs.gov/docs/ogcapi/) returned three records for `USGS-13317000`, parameter `00060`, statistic `00003`, January 1–3, 1996. Values were 7,710, 7,410 and 7,350 ft³/s; all were marked `Approved`. The API response was not chronological, so consumers must sort and check uniqueness. This validates one historical access route only. It does not map the station to a selected dam or establish a 30-year target series.

For Canada, use [Water Survey of Canada historical data/HYDAT](https://www.wateroffice.ec.gc.ca/historical_data/search_historic_data_e.html). Retain source quality flags, missing days, daily-boundary definitions and station history. Discharge inferred from a stage–discharge rating is still subject to uncertainty. Reservoir inflow, regulated release, naturalized flow and incremental flow are different targets.

## Variable definitions before retrieval

Keep forcing, derived diagnostics and model-estimated states distinguishable in metadata. AORC's native eight fields include auxiliary pressure and longwave radiation even though these are not separate rows in the requested output list. Derived fields require their inputs to be retained.

| Requested variable | Candidate route | Daily definition or qualification |
| --- | --- | --- |
| Total precipitation | AORC; Daymet comparison | Sum hourly water-equivalent amounts, then area-weight; mm per day |
| Rainfall | Derived phase partition | Preserve total precipitation; validate wet-bulb partition before use |
| Snowfall | Derived phase partition | Water equivalent, not physical fresh-snow depth |
| 2 m air temperature | AORC | Mean hourly samples, °C |
| Daily maximum temperature | AORC; Daymet comparison | Compare areal means of cellwise daily maxima; retain maximum of hourly areal mean separately |
| Daily minimum temperature | AORC; Daymet comparison | Compare areal means of cellwise daily minima; retain minimum of hourly areal mean separately |
| Snow water equivalent | ERA5-Land; station evaluation | Model estimate; daily mean or a specified state timestamp |
| Snow depth | ERA5-Land | Confirm physical-depth parameter and snow-cover convention; do not relabel SWE |
| Snowmelt | ERA5-Land diagnostic | Model-estimated water-equivalent daily amount |
| Freezing-level height | ERA5 vertical T and geopotential | Derive specified crossing; mask below-ground levels and flag no/multiple crossings |
| 0°C isotherm elevation | Same profile diagnostic | May be the same requested quantity; define altitude above sea level versus height above terrain |
| Surface soil moisture | ERA5-Land | Volumetric water content, 0–7 cm |
| Root-zone soil moisture | ERA5-Land layers 1–3 | Proposed 0–100 cm thickness-weighted mean, not a universal root-zone definition |
| Deep soil moisture | ERA5-Land layer 4 | Volumetric water content, 100–289 cm |
| Soil temperature | ERA5-Land | Preserve layer/depth; define chosen output layer before aggregation |
| Relative humidity | AORC T, q and pressure | Derive with an explicit water/ice saturation convention before daily averaging |
| Wind speed | AORC U and V | Mean of hourly speed; magnitude of mean vector is different |
| U-wind component | AORC | Mean 10 m eastward component, m/s |
| V-wind component | AORC | Mean 10 m northward component, m/s |
| Shortwave radiation | AORC downward SW | Daily mean W/m² or integrated MJ/m²; distinguish net from incoming |
| Net radiation | ERA5-Land | Net SW plus net LW; correct accumulation and sign conventions |
| Potential evapotranspiration | Defined PET method | Hargreaves–Samani for an NWRFC-style experiment; compare methods separately |
| Actual evapotranspiration | ERA5-Land diagnostic | Model estimate; verify sign and water-equivalent conversion |
| Vapor pressure deficit | AORC T, q and pressure | Derive at native time/space before averaging; kPa |
| Cloud cover | ERA5 single levels | Daily mean fraction; cloud layer definition explicit |

Daymet cell extrema cannot reconstruct extrema of the hourly basin mean. Compare like statistics with matched day boundaries; AORC extrema still reflect hourly samples. Likewise, `(tmin + tmax) / 2` is a temperature-midpoint approximation, not an observed 24-hour mean.

[ERA5-Land documentation](https://confluence.ecmwf.int/pages/viewpage.action?pageId=505384848) defines soil layers and accumulation conventions. Its hourly accumulations require deaccumulation or the documented daily endpoint method; summing cumulative steps double-counts. Its potential evaporation is not automatically equivalent to reference-crop PET. Check parameter identifiers and units for snow variables rather than relying on similar labels.

The [daily-statistics product](https://cds.climate.copernicus.eu/datasets/derived-era5-land-daily-statistics?tab=overview) omits accumulated fields such as precipitation. Use the [hourly product](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land?tab=overview) where required. ERA5-Land is a land-model replay forced by ERA5; its soil and snow fields are not direct basin-wide measurements. Pressure profiles come from [ERA5 pressure levels](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-pressure-levels), while cloud fields require the single-level product.

## Extraction contract

1. Validate the GeoJSON, CRS, unique project IDs, `part`, topology and virtual-link policy. Record geometry and input-file hashes. Use the geometry as the domain; a buffered CSV box is only a retrieval extent. Local and total catchments must not be mixed.
2. Inspect each provider's native coordinates, CRS, cell bounds, mask, calendar, packed values and units. Build cell–polygon intersection weights once per geometry/grid pair. Include fractional edge cells and holes; do not substitute the dam pixel or polygon centroid for an areal mean. For mountains, test elevation zones before reducing everything to one number.
3. Request only intersecting native-grid chunks and required time intervals. Deduplicate chunks shared by adjacent projects. Keep source URL, product version, object hash, headers, retrieval time and processing configuration. Estimate transfer/storage cost before a full run; process bounded batches rather than holding 30 years of hourly grids in memory. A coarse source does not become higher resolution when resampled.
4. Apply fill values before scale/offset, then unit and accumulation conversions. Retain hourly data through phase partitioning and nonlinear diagnostics. Track valid-area fraction and contributing hours for every project/variable/day. Strict baseline: incomplete days remain missing; an explicit, documented tolerance or imputation can be evaluated separately.
5. Write a daily table with `date`, `project_id`, source/variable identifiers, values, units, valid-area fraction, sample count and QC flags. Keep model-state estimates labeled. The proposed row count is a calendar expectation, not a requirement to fabricate missing data.
6. Align flow observations and upstream routing with the same domain and day definition. Use local polygons with local inflow plus upstream routing, or a consistent total-catchment formulation. Preserve groundwater and virtual-link assumptions separately.

## Validation before selecting the source

- **Access and coverage:** retrieve all eight AORC fields and a Daymet polygon subset for the six configured pilot projects. Include winter storms, spring snowmelt, dry summer conditions, leap days and year boundaries. Check all 26 spatial footprints and the full proposed time axis before bulk extraction is considered complete. Authenticated point access alone does not satisfy this check.
- **Consistency:** inspect missing fractions, negative precipitation, temperature bounds, radiation, water-equivalent units and date alignment. Compare monthly and water-year precipitation, temperature seasonality and storm timing across sources. Investigate source-transition years and high-elevation/border cells.
- **Observation comparison:** use appropriate GHCN/ECCC weather stations and NRCS/provincial snow stations. Some observations may already enter AORC or Daymet, so agreement is not automatically independent validation. Station snowfall undercatch and point-versus-area differences need consideration.
- **Hydrologic performance:** compare candidate forcings with the same model structure, target data, warm-up and time-blocked train/validation split. Calibrate each experiment on training data only. Report volume bias, seasonal timing, NSE/KGE and high/low-flow behavior. Evaluate regulation and snow zones where relevant. Select a source on held-out performance and coverage, not grid spacing alone.
- **Forecast use:** historical realized weather is appropriate for fitting a rainfall–runoff relationship. It is not a substitute for archived forecasts in a forecast-skill backtest. Verify future-model field availability and calibrate forecast-to-historical adjustments without using validation years.

## Reproduce the preflight

From the repository root, with `hydro-map` installed:

```bash
python research/plan_download.py --geojson outputs/catchments.geojson --output outputs/source_validation/download_plan.json
python -m unittest discover -s research -p 'test_*.py'
```

The plan reads `research/data_sources.yaml`, recalculates unbuffered areas/bounds, records hashes and reports unresolved checks. It does not contact providers, build grid weights or download meteorology.

The bounded access probes require `requests`, `numpy` and a `zstd` executable for AORC, and `curl` for Daymet. Use an environment containing the project's dependencies. Generated evidence belongs in ignored `outputs/` or `data/` directories:

```bash
pip install -r research/requirements.txt
python research/probe_aorc.py --geojson outputs/catchments.geojson --output outputs/source_validation/aorc
python research/probe_daymet.py --output outputs/source_validation/daymet
```

The AORC probe samples Mica and Brownlee; it is not a general downloader. The public Daymet probe records failures as well as catalog metadata. The [authenticated Daymet recipe](daymet_opendap.md) reproduces the successful native-grid requests. None establishes a complete 30-year dataset. ERA5 sample access and the full polygon/time coverage checks remain necessary before a production downloader is added.
