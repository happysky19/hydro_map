# Historical forcing source assessment

Checked 6 October 2026. Research status: **AORC is the leading candidate; no source has passed full-period, full-catchment validation.** These scripts are separate from the production CLI.

## Decision

Start with AORC 1.1 for the core meteorological forcing over both US and Canadian catchments. Compare precipitation and temperature with Daymet V4 R1. Evaluate ERA5-Land as a consistent alternative forcing experiment and a source of supplementary land states; use ERA5 for atmospheric fields unavailable in the other products. Do not splice sources at the international border without evaluating the resulting discontinuity.

The requested extraction period is **1996–2025**, with 10,958 Gregorian days and 471,194 project-day rows for 43 independent local drainage domains. All 262,992 AORC hourly time-coordinate values in this window have been checked. The required precipitation boundary at 2026-01-01 00Z is absent from the inspected archives, so the final daily precipitation value remains blank with a quality flag. Earlier audits used 26 projects, whole-unit boundaries or 41 forcing groups with two shared pairs and, in some cases, 1995–2024; those results remain historical evidence, not validation of the full current selection and window. Final day boundaries must match the selected flow target. A complete time axis does not prove that every data cell is present. See [download and daily aggregation](download_demo.md) for the current December 29–31, 2025 replays, their validation status, units and limitations.

Kootenay Canal's local domain covers natural drainage at the tailrace, including lateral runoff to the bypassed river. Its separate diversion from Corra Linn requires operational flow allocation; local weather does not determine turbine inflow. The geometry, plan and meteorological run metadata retain the intake project and this interpretation.

See [documented model applications](model_data_sources.md) for seven examples of the actual forcing used by NWRFC, NWM, SAC-SMA, LSTM, VIC and Canadian models. The same model structure can use different forcings. NWRFC's calibration also adjusts monthly forcing climatology; matching its data source alone does not reproduce its forecast skill.

| Source | Proposed use | Evidence obtained | Outstanding issue |
| --- | --- | --- | --- |
| NOAA AORC 1.1, hourly, 30 arc seconds | Core forcing; preserve hourly inputs before daily aggregation | 1996–2025 time axis checked; December 29–31, 2025 cache replay validated for 43 independent domains: 1,505 valid daily statistics of 1,548, with only the 43 final precipitation totals missing; earlier native NOAA comparison matched four fields | Full-period data masks and values; global Zarr correction remains unconfirmed |
| NASA/ORNL Daymet V4 R1, daily, 1 km | Temperature and precipitation comparison across the border | Authenticated full-polygon precipitation/Tmin/Tmax extraction and explicit leap-year alignment | Full-period data coverage, remaining variables, native local-day versus UTC comparison |
| Copernicus ERA5-Land, hourly, native about 9 km | Alternative experiment; snow and soil estimates | Public NCAR cache replay: 43 independent domains, temperature/precipitation/SWE, December 29–31, 2025; all 645 daily statistics valid | NCAR mirror starts in November 2001 and lacks soil states; complete CDS archive access remains unverified |
| Copernicus ERA5, hourly | Cloud cover and vertical temperature profiles | Official single-level and pressure-level products identified | Same access and validation gates; coarser terrain representation |
| USGS / ECCC HYDAT / reservoir operators | Streamflow, reservoir inflow and regulation targets | Three approved historical USGS daily-flow records retrieved | A separate station/series mapping for every project; Canadian and operator access untested |
| NRCS SNOTEL / provincial snow observations | Snow evaluation at station locations | Official observing programs identified | Station inventory, representativeness, gaps and measurement history |

An official source can still contain interpolation error, model bias or missing records. Successful HTTP access establishes availability of that response, not hydrologic skill.

## What transfers from NWRFC

The [2026 NWRFC calibration paper](https://repository.library.noaa.gov/view/noaa/73306/noaa_73306_DS1.pdf), especially Sections 2–3, describes AORC precipitation, temperature, specific humidity and surface pressure. Wet-bulb temperature supports rain/snow partitioning; zone temperature supports Hargreaves–Samani PET. SNOW-17, SAC-SMA and routing represent snow, soil and flow processes. The case studies use six-hour integration and daily calibration objectives. This supports retaining hourly meteorology even when the final requested table is daily. It does not establish that every current operational input is AORC or that an arbitrary HydroBASINS polygon reproduces an NWRFC zone.

The [NWRFC calibration repository](https://github.com/NOAA-NWRFC/nwsrfs-hydro-autocalibration) expects zone MAP, MAT and PTPS, and separate upstream flow for routing. Its time-step conventions also differ between forcing and simulated flow. Therefore local precipitation cannot, on its own, explain a downstream dam's release. Upstream flow, storage changes, diversions and the target's meaning must be explicit.

NWRFC publishes [operational forcing and model-output downloads](https://www.nwrfc.noaa.gov/misc/downloads/index.php?filter=&sortasc=true&sortby=date&type=forecast_precipitation_points). No complete 30-year operational forcing archive for the earlier 26-project selection was verified here. NOAA also maintains an [AORC NWRFC regional NetCDF archive](https://hydrology.nws.noaa.gov/pub/AORC/V1.1/NWRFC_1km/), packaged as monthly TAR files. The inspected regional files contain precipitation, temperature, specific humidity and pressure. Individual files were retrieved successfully with bounded HTTP Range requests, without downloading whole monthly TARs. The listing reaches December 2025; the January 2026 TAR returned 404.

## Access tests and limitations

### AORC

Anonymous requests to `noaa-nws-aorc-v1-1-1km` succeeded. The root listed 1979–2025. Sampled coordinate arrays span 20–54.9986°N and 130–60.0028°W, including masked padding. Use those coordinate arrays and masks, not an approximate catalog extent, to locate cells. Expected padding outside the data domain is distinct from the announced masking defect.

An initial point probe decoded twelve complete meteorological chunks: two variables, three locations, two years. Each point had 144 valid hourly temperature and precipitation values for January 1–6. These are grid-cell access checks, not catchment-average results.

| Sample cell | Longitude | Latitude | 1996 Jan 1 mean T / total P | 2025 Jan 1 mean T / total P |
| --- | ---: | ---: | ---: | ---: |
| Mica northern tip | −118.992107 | 52.865352 | −12.246°C / 1.3 mm | −15.992°C / 3.0 mm |
| Mica interior | −117.183846 | 51.498740 | −10.254°C / 0.0 mm | −4.092°C / 0.7 mm |
| Brownlee interior | −114.000640 | 43.174073 | −1.971°C / 0.0 mm | −8.183°C / 1.6 mm |

All three centers fall inside their catchments. The later spatial audit covered every positive-area polygon/cell intersection for all 26 catchments: 1,047,189 intersections across 55 unique spatial chunks. Every intersection had valid values for all eight fields at **1995-01-01 00Z** and **2025-01-01 00Z**. At the latter timestamp, precipitation, temperature, specific humidity and pressure matched the original NOAA NetCDF masks and packed integer values exactly, with zero mismatches. Native NetCDF also supplied full valid coverage for those four fields at 1996-01-12 23Z. No same-hour native comparison was performed for the 1995 sample.

This audit used bounded Zstd chunk prefixes to decode the complete first hourly plane, with cross-checks against complete cached chunks. A truncated prefix is not a complete Zarr-object integrity check. Mask-area fractions used EPSG:6933; canonical polygon areas use the WGS84 ellipsoid. These results establish sampled spatial coverage, not all-hour completeness.

Every time-coordinate value for 1995–2024 was decoded: **262,992 consecutive hourly timestamps**, with no gaps, duplicates or disorder. All eight variables' metadata dimensions match the complete annual coordinate axes. The first 144 hours of 2025 were also decoded to check the end boundary. This checks coordinates and dimensions; it does not scan all meteorological values.

The [official catalog](https://registry.opendata.aws/noaa-nws-aorc/) still carries an April 2, 2026 notice about incorrectly masked rows, estimated at 0.04%, with corrected Zarr files being regenerated. Its inspected change history does not announce completion. The sampled hours show no local discrepancy, but they cannot establish that the defect is absent throughout 30 years. The extraction policy is to check every required cell/time, reject incomplete daily results, and compare any suspicious masking with native NOAA files. Never silently renormalize around missing cells or fill them with zero. This resolves how to detect and handle the defect; its global repair status remains an external uncertainty.

AORC precipitation is an hourly amount ending at its timestamp. UTC January 1 requires hours ending January 1 01Z through January 2 00Z. The requested 1996–2025 window therefore uses precipitation timestamps from 1996-01-01 01Z to 2026-01-01 00Z, inclusive. The inspected 2025 archive ends at December 31 23Z; neither the Zarr 2026 prefix nor the regional native archive supplied the next boundary. Never silently total 23 hours as a complete day. Temperatures are instantaneous: the mean of 00–23Z samples approximates a daily mean, and sampled maxima/minima are not continuous-time extrema.

The [NOAA methods report](https://hydrology.nws.noaa.gov/pub/AORC/V1.1/Documents/AORC-Version1.1-SourcesMethodsandVerifications.pdf) includes Canadian contributing watersheds and Columbia climatology processing. Coverage therefore need not stop at the border, but method and observation-density changes still need evaluation. Include the 2015–2016 transition in auxiliary meteorological fields in consistency tests.

### Daymet

The [current NASA guide](https://data.ornldaac.earthdata.nasa.gov/public/daymet/Daymet_Daily_V4R1/comp/Daymet_Daily_V4R1.pdf), revised May 22, 2026, identifies the 2025 release. Live CMR queries returned all seven named North American annual variables for 1996 and 2025, and none for 2026. V4 R1 repaired Canadian January input gaps in 2020–2021. Canadian station inputs changed in 2024–2025 because of feed problems; assess temporal consistency around those years.

Two public single-pixel requests, Mica/1996 and Brownlee/2025, each timed out after 55 seconds with zero payload bytes. An unauthenticated OPeNDAP metadata request required Earthdata Login and returned HTTP 401. These failures do not establish that the data are absent. Following the [current OPeNDAP tutorial](https://github.com/ornldaac/daymet/blob/main/tutorials/daymet_subset_opendap.ipynb), authenticated access then succeeded: metadata (7,044 bytes), native coordinate axes (24,888 bytes), and two precipitation subsets (44,541 and 44,596 bytes), all HTTP 200.

Those initial point subsets cover February 28–March 1, 1996 and December 29–31, 2025. Decoded 1996 day-of-year values 59, 60 and 61 confirm February 29 is retained. The files explicitly label local 24-hour days; their noon CF time coordinates must not be interpreted as UTC event times.

`probe_daymet_polygons.py` now retrieves native precipitation, Tmin and Tmax and intersects native 1-km cells with whole local polygons. It uses WGS84 geodesic intersection areas, including fractional edge cells and holes. Pilots cover Mica, Arrow, Albeni Falls, Brownlee, Lower Granite, The Dalles and Oxbow on February 28–March 1 and December 30, 1996, plus January 1, 1997: **105 of 105 project/variable/date results have 100% valid area**. Oxbow has 567 intersecting cells, including 144 fractional edges; Mica has 20,356, including 1,573 fractional edges. The 65 responses total 18,130,498 bytes. Native-array checks found no missing values or negative precipitation, and Tmin never exceeded Tmax across 1,675,240 intersected cell-days per variable. An offline cache replay reproduced the results with network access disabled. All 26 polygons are inside the native grid rectangle; data-mask coverage remains untested for the other 19 projects. AppEEARS remains an alternative interface, not a required dependency.

Daymet has 365 records every year, including February 29 and omitting December 31 in leap years. The target window therefore has 10,950 Daymet records and eight missing Gregorian dates. Retain those dates with missing-value flags in a joined table; do not shift the remaining days or quietly interpolate precipitation. Its `srad` is a daylight mean: multiply by `dayl` for daily energy, or by `dayl/86400` for a 24-hour mean. [Calendar and variable definitions](https://daymet.ornl.gov/overview)

Daymet SWE is a simple model estimate, not independent snow sensing. The [methods paper](https://pmc.ncbi.nlm.nih.gov/articles/PMC8302764/) cautions against treating it as a general-purpose snowpack model. The daily weather convention targets local days, which requires reconciliation with UTC forcing and station flow days before event-scale comparisons.

### ERA5-Land

The official [NCAR GDEX subset](https://gdex.ucar.edu/datasets/d633008/) provides anonymous native-grid subsetting. Three requests returned 4,667,392 bytes in total, covering 25 hourly timestamps from 2024-12-31 00Z to 2025-01-01 00Z. Temperature, total precipitation and SWE were decoded and area-weighted over all 26 polygons; every one of the 78 daily results had complete valid-area coverage. This verifies a public access route and one-day processing, not all variables or 30-year completeness.

The returned `tp` is cumulative forecast precipitation in metres. The 2024-12-31 total uses the **2025-01-01 00Z endpoint multiplied by 1,000**, not a sum of cumulative hourly steps. The `sd` field's long name says “Snow depth,” but parameter 141 and units `m of water equivalent` identify SWE. It must not be presented as physical snow depth. Temperature and SWE use the mean of 00–23Z samples; a missing state hour produces a missing daily result.

The catalog title says “1950 to present,” but this mirror's actual temporal range begins **2001-11-01 01Z**. Its listed subset also lacks soil-moisture and soil-temperature fields. It cannot be the sole source for the proposed 30-year, full-variable experiment. The complete [CDS ERA5-Land archive](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land?tab=overview) remains the intended route for those fields and earlier years. CDS credentials are not configured in the inspected environment, so that route is not yet verified. Credentials should be configured locally, never placed in repository files or reports.

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

- **Access and coverage:** extend the successful polygon pilots to all required data cells and timestamps. Include spring snowmelt, dry summer conditions and source-transition years in addition to the audited winter/leap/year-boundary samples. All eight AORC fields have sampled all-project coverage; the complete time axis has been checked. Full-period meteorological data validity is still pending.
- **Consistency:** inspect missing fractions, negative precipitation, temperature bounds, radiation, water-equivalent units and date alignment. Compare monthly and water-year precipitation, temperature seasonality and storm timing across sources. Investigate source-transition years and high-elevation/border cells.
- **Observation comparison:** use appropriate GHCN/ECCC weather stations and NRCS/provincial snow stations. Some observations may already enter AORC or Daymet, so agreement is not automatically independent validation. Station snowfall undercatch and point-versus-area differences need consideration.
- **Hydrologic performance:** compare candidate forcings with the same model structure, target data, warm-up and time-blocked train/validation split. Calibrate each experiment on training data only. Report volume bias, seasonal timing, NSE/KGE and high/low-flow behavior. Evaluate regulation and snow zones where relevant. Select a source on held-out performance and coverage, not grid spacing alone.
- **Forecast use:** historical realized weather is appropriate for fitting a rainfall–runoff relationship. It is not a substitute for archived forecasts in a forecast-skill backtest. Verify future-model field availability and calibrate forecast-to-historical adjustments without using validation years.

## Reproduce the preflight

From the repository root, with `hydro-map` installed:

```bash
python research/plan_download.py --geojson outputs/projects43_independent/dam_catchments.geojson --output outputs/projects43_independent/download_plan.json
python -m unittest discover -s research -p 'test_*.py'
```

The plan reads `research/data_sources.yaml`, recalculates unbuffered areas/bounds, records hashes and reports unresolved checks. It does not contact providers, build grid weights or download meteorology.

### Earlier 26-project access probes

The following probes refer to the earlier geometry file and project IDs. For
current 43-project extraction, use [the download workflow](download_demo.md).

The bounded access probes require `requests`, `numpy` and a `zstd` executable for AORC, and `curl` for Daymet. Use an environment containing the project's dependencies. Generated evidence belongs in ignored `outputs/` or `data/` directories:

```bash
pip install -r research/requirements.txt
python research/probe_aorc.py --geojson outputs/catchments.geojson --output outputs/source_validation/aorc
python research/probe_daymet.py --output outputs/source_validation/daymet
python research/probe_daymet_polygons.py --geojson outputs/catchments.geojson --output outputs/source_validation/daymet_polygons --projects MICA ARROW ALBENI_FALLS BROWNLEE LOWER_GRANITE THE_DALLES OXBOW
python research/probe_era5_land.py --geojson outputs/catchments.geojson --output outputs/source_validation/era5_land
```

The original AORC probe samples Mica and Brownlee; it is not a general downloader. The public Daymet probe records failures as well as catalog metadata. The [authenticated Daymet recipe](daymet_opendap.md) describes Earthdata access; the polygon probe uses the same credentials without logging them. Polygon probes accept `--cache-only`, verify cached response hashes and mark a run incomplete before processing. Daymet alignment preserves leap-year December 31 as `daymet_calendar_gap`; there is no imputation. None of these probes establishes a complete 30-year dataset.

The [offline AORC audit](aorc_audit.md) rebuilds calendar and sampled spatial checks from saved source payloads, validates their SHA-256 hashes, and compares native NOAA packing and values. It does not download the full meteorological record. Its evidence directories must be retained locally; generated payloads are excluded from Git.
