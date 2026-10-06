# hydro-map

Delineate selected-project catchments from HydroSHEDS flow directions and compare polygon layers on maps.

## Quick start

Run from this directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[maps]'
hydro-map download --config configs/columbia.yaml --cache-dir data
hydro-map build configs/columbia.yaml --cache-dir data --output outputs/projects43_independent/dam_catchments.geojson --csv-output outputs/projects43_independent/polygon_grid_bbox.csv --table-output outputs/projects43_independent/dam_catchments.csv
hydro-map plot outputs/projects43_independent/dam_catchments.geojson --project MICA --labels HydroSHEDS --basemap terrain --output figures/mica.png
```

The example selects **43 Pacific Northwest projects**, including Columbia basin projects and Ross on the Skagit. It is an editable selection, not a complete dam inventory. Change the YAML project list to define the upstream cutoffs for your application. `HUGH_KEENLEYSIDE` is the canonical ID for the project previously named `ARROW`; its tabular `project_code` remains `ARDB`.

The example uses **HydroSHEDS 15 arc-second flow directions**, tracing every cell that drains to a documented outlet cell. HydroBASINS level 12 supplies a search envelope and location checks; its whole subbasins do not determine the final boundary. `build` downloads and caches the North America direction and accumulated-area rasters when needed.

The 43 project records represent **43 distinct local drainage domains**. Seven Mile and Waneta use their own dam outlets. Corra Linn uses its dam outlet; Kootenay Canal uses a downstream river control point at the canal return. No example projects share a polygon. The Canal polygon describes natural runoff generated between Corra Linn and the return reach, including lateral runoff to the bypass river. Turbine inflow also depends on upstream diversion and operating decisions. See [outlet sources and limitations](docs/project_outlets.md).

## Historical daily data

Install the research dependencies, configure `~/.cdsapirc` as described below,
and run one command for all three sources:

```bash
python -m pip install -e . -r research/requirements.txt
python research/download_daily.py --geojson outputs/projects43_independent/dam_catchments.geojson --start 2025-12-29 --end 2025-12-31 --output outputs/catchment_daily.csv
```

This downloads AORC, ERA5-Land and ERA5, computes daily catchment statistics
and derived variables, and exports `catchment_daily.csv` with 47 columns
(date, project ID and 45 values), `catchment_daily_qc.csv` with 182 columns
(the same keys and per-variable quality fields), and
`catchment_daily.csv.manifest.json` with units and provenance. Both tables
have the same 129 project-day rows in this example. Keep all three files.
Repeat the same command to resume completed source periods. AORC also needs
the `zstd` command-line decoder on `PATH`.

The [data pipeline guide](docs/data_pipeline.md) covers CDS token setup, AORC
and ERA5/ERA5-Land downloads, derived variables, units, quality flags and final
CSV/Parquet delivery. The research scripts preserve one source per variable
and export one row per project and UTC date. Start with a short sample before
running the full 1996-2025 period.

## Build options

```bash
hydro-map build configs/columbia.yaml --cache-dir data --output outputs/total.geojson --part total
hydro-map build configs/columbia.yaml --cache-dir data --output outputs/local.geojson --part local --exclude-virtual
hydro-map build configs/columbia.yaml --cache-dir data --output outputs/two_projects.geojson --projects MICA REVELSTOKE
hydro-map build configs/columbia.yaml --source data/hybas_na_lev12_v1c/hybas_na_lev12_v1c.shp --output outputs/from_local.geojson
hydro-map build configs/columbia.yaml --cache-dir data --flow-direction data/hydrosheds/hyd_na_dir_15s/hyd_na_dir_15s.tif --flow-accumulation data/hydrosheds/hyd_na_aca_15s/hyd_na_aca_15s.tif --output outputs/from_rasters.geojson
```

`local` subtracts the upstream catchments of selected outlet groups from each group's total upstream catchment. `total` retains the entire upstream catchment. Shared projects do not subtract each other; both receive the same explicitly marked group polygon. `--projects` filters output only: every project in the YAML remains an upstream cutoff when calculating local catchments. Adding upstream projects can therefore change existing local boundaries; rebuild geometry and weather weights after changing the selection.

`--flow-direction` and `--flow-accumulation` are paired options for existing compatible DIR and ACA GeoTIFFs. Omit both to use the automatic download/cache. ACA is the HydroSHEDS accumulated-area layer in hectares, not the ACC cell-count layer.

The `outlet_cell` method follows physical D8 routing and excludes virtual endorheic connections. `--include-virtual` is rejected in this mode. A terminal inland sink does not become an upstream contributing area merely because HydroBASINS provides a virtual connection.

For an explicit legacy approximation, set `dataset.delineation: outlet_unit` in a separate configuration. That method combines whole HydroBASINS outlet units and their upstream network; it can include land draining below a dam. In that mode, `--include-virtual`, `--exclude-virtual`, and `dataset.include_virtual_connections` control the network policy.

## Project configuration

Each project specifies a unique `id`, public `name`, `river`, country code, and an `outlet` with `hybas_id`, geographic `lon` and `lat`, a public `source`, and a description of the coordinate `reference`. The `outlet_cell` method also requires explicit `grid_lon`, `grid_lat`, and `grid_reference` fields for the selected HydroSHEDS pixel center. These distinguish the source location from the raster cutoff; the build does not search for a cell that reproduces a reported drainage area.

An optional unique `project_code` supplies the CSV identifier. It is retained in GeoJSON properties; the GeoJSON `id` and network relationships keep using the canonical project ID. Without `project_code`, the CSV uses `id`.

```yaml
dataset:
  region: na
  level: 12
  version: '1c'
  include_virtual_connections: false
  delineation: outlet_cell
projects:
  - id: MICA
    project_code: MCDB
    name: Mica
    river: Columbia
    country: CA
    outlet:
      hybas_id: 7120225080
      lon: -118.569792
      lat: 52.077993
      source: https://doi.org/10.6084/m9.figshare.25988293
      reference: river-aligned dam reference point
      grid_lon: -118.5687500000
      grid_lat: 52.0770833333
      grid_reference: HydroSHEDS 15s cell containing the published river-aligned dam reference
```

The example uses published Global Dam Watch river-aligned points and documented government locations. Cabinet Gorge, Wells, Little Goose, Lower Monumental, Rocky Reach, and Chief Joseph use USACE National Inventory of Dams coordinates in place of coarse or monitoring references. Corra Linn uses the [BC official approximate dam centre](https://apps.gov.bc.ca/pub/bcgnws/names/51921.html). Kootenay Canal uses a distinct river-return control cell downstream of the powerhouse location linked by [BC Hydro](https://www.bchydro.com/community/recreation_areas/kootenay_canal.html). These selections follow documented locations and drainage connections, not reported areas. A river-aligned or inventory point is not necessarily a surveyed dam-wall position. Source and interpretation are retained in the configuration. The Wells reference-to-grid distance is about 829 m and warrants particular attention when evaluating finer boundaries.

The configured point must fall within its specified HydroBASINS unit. A mismatch fails validation; points are not automatically snapped to a nearby unit. Review the reference and unit together before changing either.

In `outlet_cell` mode, projects can use distinct river cells within the same HydroBASINS unit and remain independent. An explicit `outlet_group` combines projects instead. Group names must differ from project IDs, and a group cannot span different units. Member outlet cells must be on one downstream path; the downstream-most cell defines their shared polygon. The legacy `outlet_unit` mode requires projects in the same unit to declare a shared group. This remains a shared approximation even if their individual cells are distinct. Descriptive `kind` and `mw` values are configured inputs; they are not used to draw the polygons or independently audited as current plant capacities.

A diversion return requires `catchment_role: natural_reach_at_tailrace`, a configured `diversion_intake_project`, and `routing_requires_operations: true`. Its intake must be upstream in the natural D8 network. The return remains an independent weather-aggregation domain. Forecasting turbine flow requires a separate allocation model using diversion, spill, storage and operation data; this repository only prepares boundaries and meteorological inputs.

A raster outlet cut can split a HydroBASINS unit. The reverse-flow traversal also excludes any tributary that joins below the chosen cell, rather than retaining all upstream units automatically. Resolution remains 15 arc-seconds: these are derived grid catchments, not certified dam-wall surveys or a model of engineered diversions.

## GeoJSON output

Output uses the standard `FeatureCollection` → `features[]` → `Feature` structure, with `properties` and `geometry` on each feature. Coordinate positions are **[longitude, latitude]** in WGS84 decimal degrees. Geometry can be `Polygon` or `MultiPolygon`; disconnected pieces are preserved. Each feature carries:

| Property | Meaning |
| --- | --- |
| `id`, `name`, `kind`, `mw` | Configured identifier, name, operational category and capacity; unknown category/capacity is null |
| `lat`, `lon` | Configured reference-point coordinates |
| `area` | Total upstream group area, in km²; identical to `area_total` |
| `area_local` | Selected-project local catchment area, in km² |
| `area_total` | Total upstream group catchment area, in km² |
| `area_geometry` | Area of the actual emitted geometry, in km²; equals `area_local` for a local layer |
| `up` | Direct selected upstream IDs in the natural drainage network |
| `above` | All selected upstream project IDs |
| `down` | Nearest selected downstream ID in the natural drainage network when unique, otherwise null |
| `down_candidates` | Downstream project IDs at the next selected group; multiple IDs identify a shared group |
| `part` | `local` or `total` |
| `forcing_group` | Unique catchment key for spatial aggregation and water-balance accounting |
| `geometry_status` | `outlet_cell_delineation` for an ordinary raster catchment; `shared_unit_approximation` for shared groups |
| `shared_outlet_projects` | IDs sharing this forcing domain, or an empty list for an independent project |
| `hybas_id` | HydroBASINS outlet-reference index; it does not identify the raster catchment boundary |
| `outlet_source`, `outlet_reference` | Reference-point provenance and interpretation |
| `catchment_role` | `natural_reach_at_tailrace` identifies an incremental river reach at a diversion return |
| `diversion_intake_project` | Project at the upstream diversion intake; separate from the natural drainage links |
| `routing_requires_operations` | `true` explicitly identifies a need for operational flow allocation; absent means unspecified |

Areas are calculated afresh from the corresponding total/local polygons on the WGS84 ellipsoid. They are not copied from HydroRIVERS attributes or the source dataset's rounded area fields. Native HydroSHEDS ACA values are hectares accumulated with the publisher's grid-area weights; divide them by 100 for km². They are a separate diagnostic and may differ slightly from ellipsoidal polygon areas for the same cells. Keep the area method attached to any comparison. Relationships describe natural drainage between selected control points. They do not allocate water between a canal and the bypass river.

**Raster delineation uses schema version 3; legacy unit delineation uses version 2.** Both use `area` for total upstream group area. Versions before 2 used `area` for the emitted geometry. Use `area_geometry`, or recompute from `geometry`, for the actual layer area. Plotting and bounding-box export recompute geometry area, so they do not depend on a stored `area` interpretation. Metadata records `schema_version` and `area_property`.

Collection metadata records source datasets and hashes, the delineation method, virtual-connection policy, local cutoffs, and area method. Raster output records HydroBASINS source hashes and any repaired envelope-unit IDs under `search_envelope`; those repairs concern the search envelope rather than the raster catchments. It omits `hybas_ids` and `unit_count` because its boundary is not an aggregation of whole units. Configured descriptive fields such as `river` and `country` are retained on features.

The common property contract is `id`, `name`, `kind`, `mw`, `lat`, `lon`, `area`, `area_local`, and `part`, plus `geometry` on the same GeoJSON feature. Additional topology and provenance fields are retained. `area_hydrorivers` and `divisions` require their own source data and definitions; this workflow does not substitute computed polygon area or empty objects for unknown values. Consumers should accept additional properties and collection metadata. Matching the structure does not imply identical boundaries or numeric values.

`lat` and `lon` identify the configured outlet reference point. They are distinct from the polygon centroid exported in the CSV. The feature `id` and network links retain canonical project IDs; `project_code` supplies the optional tabular identifier.

All 43 geometries and attributes are in **one GeoJSON file**. An external reference file can use the same standard FeatureCollection layout. With optional GeoPandas installed, both can be inspected identically:

```python
import geopandas as gpd

gdf = gpd.read_file("outputs/projects43_independent/dam_catchments.geojson")
print(gdf[["id", "name", "kind", "mw", "lat", "lon", "area", "area_local", "part", "geometry"]])
print(gdf["id"].nunique())
print(gdf.iloc[0]["geometry"])
```

The final line displays WKT such as `POLYGON ((...))`; GeoJSON itself stores coordinate arrays, not a WKT string. Coordinates are always longitude first. For an external file, verify the meaning of `area` and its CRS; map comparisons calculate areas directly from the polygons.

`--table-output outputs/projects43_independent/dam_catchments.csv` additionally writes the common attributes and WKT `geometry` in each row of a single CSV. It also includes `area_total`, `area_geometry`, shared-group fields, and the three diversion-interpretation fields. This is different from the bounding-box CSV below: the geometry table contains the full boundary. Plotting reads this CSV directly. For GeoPandas use `pandas.read_csv`, then `geopandas.GeoSeries.from_wkt(df.pop("geometry"), crs="EPSG:4326")` to construct its spatial column. Continue using the GeoJSON for forcing downloads.

## Bounding-box CSV

Add `--csv-output` to `build` to export the same selected geometries as a table:

```bash
hydro-map build configs/columbia.yaml --output outputs/catchments.geojson --csv-output outputs/polygon_grid_bbox.csv --bbox-buffer 0.3
```

Columns, in order: `ProjectCode`, `PolygonName`, `MinLatitude`, `MaxLatitude`, `MinLongitude`, `MaxLongitude`, `CentroidLatitude`, `CentroidLongitude`, `AreaKm2`, `BufferDegreesApplied`.

Rows follow YAML order. `PolygonName` combines the project name with `local incremental catchment` or `total upstream catchment`, according to `--part`. A local `natural_reach_at_tailrace` polygon uses `local natural reach catchment at tailrace` to distinguish its drainage meaning. The CSV uses the same selected-project cutoffs and virtual-connection setting as its companion GeoJSON.

Shared-unit rows instead include `shared local catchment [GROUP]` or `shared total catchment [GROUP]` in `PolygonName`, retaining the same ten columns while making duplicated areas visible in the standalone table.

Area is recalculated on the WGS84 ellipsoid from the unbuffered geometry and written to one decimal place. Centroids use the unbuffered polygon in the longitude/latitude plane, rounded to four decimals; these coordinates are polygon centroids, not dam reference points or geodesic centers.

Only the bounding box is expanded by `--bbox-buffer` degrees on each side (default `0.3`; use `0` for no padding). Bounds are clipped to geographic coordinate limits and rounded outward to four decimals so the exported extent covers the padded geometry. Outward rounding can differ from nearest rounding by `0.0001°`. `BufferDegreesApplied` records the supplied padding without forcing one-decimal precision. Buffering does not change area or centroid.

## Compare maps

```bash
hydro-map plot one.geojson two.geojson --project MICA --labels A B --output figures/mica.png
hydro-map plot one.geojson two.geojson --project MICA --labels A B --basemap light --output figures/mica_map.png
hydro-map plot outputs/projects43_independent/dam_catchments.geojson data/reference/catchments.csv --project MICA --labels HydroSHEDS Reference --basemap terrain --output figures/mica_comparison.png
hydro-map plot outputs/projects43_independent/dam_catchments.geojson data/reference/catchments.csv --by-project --labels HydroSHEDS Reference --output-dir figures/comparison
hydro-map plot outputs/catchments.geojson --project MICA --basemap terrain --coordinates lonlat --output figures/mica_terrain.png
hydro-map plot outputs/catchments.geojson --project MICA --coordinates projected --output figures/mica_projected.pdf
hydro-map plot outputs/catchments.geojson --by-project --output-dir figures
```

Plotting accepts GeoJSON, CSV with WKT geometry, and shapefiles with their accompanying `.prj` and component files. Formats can be mixed in one command. Use matching project identifiers to compare the same project across layers.

A CSV must have a `geometry` column containing complete WGS84 `POLYGON (...)` or `MULTIPOLYGON (...)` text, with longitude before latitude. Use `id` and `name` columns, or choose alternatives with `--id-field` and `--name-field`. Geometry cells containing commas must be quoted according to CSV rules; standard Pandas/GeoPandas CSV export does this automatically. UTF-8 files with or without a BOM and large geometry cells are supported. Other CSV attributes remain strings. A bounding-box-only CSV cannot reproduce the catchment outline and is rejected. Compare the same `part` and upstream cutoff selection; the plotter does not infer or reconcile their meanings.

Each title shows the project name and each layer's area recalculated on the WGS84 ellipsoid. Layers use distinct outlines and light translucent fills, with a local geodesic scale bar, north arrow, and dam-reference markers. Stored `area` properties do not affect the calculation. `--title` changes the heading; `--id-field` and `--name-field` select alternative attribute names. Save as PNG, SVG, or PDF by choosing the output extension.

`--coordinates lonlat` is the default: longitude and latitude axes have degree and hemisphere labels, with aspect corrected at the map's central latitude. `--coordinates projected` uses a local Lambert azimuthal equal-area projection and shows kilometre ticks. Display projection never changes the input GeoJSON or the ellipsoidal area calculation.

`--basemap terrain` adds Esri World Topographic Map tiles; `--basemap light` adds Esri World Light Gray Canvas tiles. Install the optional mapping dependencies with `pip install -e '.[maps]'`. Tiles are reprojected to the plot CRS, cached under `data/map_tiles` (change with `--tile-cache`), and attributed inside the map's lower-right corner. The first request for an uncached tile needs network access. Keep publisher attribution on shared figures and follow the provider's usage terms. Background maps provide visual context; they do not define catchment boundaries.

The default `--basemap none` needs no tile service or mapping extra. Use `pip install -e .` for this offline plotting setup. A failed basemap request reports an error; retry it or choose `--basemap none` explicitly. Details of tile reprojection are in the [contextily documentation](https://contextily.readthedocs.io/en/latest/warping_guide.html).

Plot inputs must contain valid Polygon or MultiPolygon geometries. GeoJSON and WKT CSV coordinates must be WGS84; shapefiles are reprojected from their `.prj`. Batch mode requires the same project IDs across all inputs and writes one PNG per project plus an overview. It refuses to overwrite existing project figures; use a new output directory for another comparison.

## Tests

```bash
python -m unittest discover -s tests
```

Tests use small local fixtures and do not require downloading the regional dataset. Downloaded data, generated boundaries, and figures are excluded from version control.

## Data attribution and terms

HydroBASINS is published through [HydroSHEDS](https://www.hydrosheds.org/products/hydrobasins). The example downloads the [North America standard level 12 version 1c archive](https://data.hydrosheds.org/file/hydrobasins/standard/hybas_na_lev12_v1c.zip). Attribute HydroBASINS and cite Lehner and Grill (2013), [Global river hydrography and network routing](https://doi.org/10.1002/hyp.9740). HydroBASINS uses the HydroSHEDS core-product license: consult the product page's linked license agreement and [publisher terms](https://www.hydrosheds.org/terms-of-use) for attribution, redistribution, and use conditions.

The raster workflow uses official [HydroSHEDS core downloads](https://www.hydrosheds.org/hydrosheds-core-downloads): [North America DIR 15s](https://data.hydrosheds.org/file/hydrosheds-v1-dir/hyd_na_dir_15s.zip) and [ACA 15s](https://data.hydrosheds.org/file/hydrosheds-v1-aca/hyd_na_aca_15s.zip). The [technical documentation](https://data.hydrosheds.org/file/technical-documentation/HydroSHEDS_TechDoc_v1_4.pdf) defines D8 encoding, area units, and the license. Cite Lehner, Verdin and Jarvis (2008), [New global hydrography derived from spaceborne elevation data](https://doi.org/10.1029/2008EO100001).

Published dam-reference coordinates come from [USACE National Inventory of Dams](https://nid.sec.usace.army.mil/nid/), [Global Dam Watch dataset metadata](https://doi.org/10.6084/m9.figshare.25988293), [NOAA NWRFC station metadata](https://www.nwrfc.noaa.gov/river/site_meta_csv.cgi), and the government documents linked in the YAML. Retain those source links and follow the respective publishers' data licenses and attribution requirements when redistributing reference data or derived products.
