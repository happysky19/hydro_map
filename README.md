# hydro-map

Build selected-project catchments from HydroBASINS topology and compare polygon layers on maps.

## Quick start

Run from this directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[maps]'
hydro-map download --config configs/columbia.yaml --cache-dir data
hydro-map build configs/columbia.yaml --cache-dir data --output outputs/projects43/dam_catchments.geojson --csv-output outputs/projects43/polygon_grid_bbox.csv --table-output outputs/projects43/dam_catchments.csv
hydro-map plot outputs/projects43/dam_catchments.geojson --project MICA --labels HydroBASINS --basemap terrain --output figures/mica.png
```

The example selects **43 Pacific Northwest projects**, including Columbia basin projects and Ross on the Skagit. It is an editable selection, not a complete dam inventory. Change the YAML project list to define the upstream cutoffs for your application. `HUGH_KEENLEYSIDE` is the canonical ID for the project previously named `ARROW`; its tabular `project_code` remains `ARDB`.

The 43 project records represent **41 distinct catchments**. Kootenay Canal/Corra Linn and Seven Mile/Waneta each share an explicitly declared outlet group because HydroBASINS level 12 does not resolve them separately. Their duplicated polygons are marked `geometry_status: shared_unit_approximation` and carry the same `forcing_group`. Count each group once when summing area or water volume. See [outlet sources and limitations](docs/project_outlets.md).

## Build options

```bash
hydro-map build configs/columbia.yaml --cache-dir data --output outputs/total.geojson --part total
hydro-map build configs/columbia.yaml --cache-dir data --output outputs/local.geojson --part local --exclude-virtual
hydro-map build configs/columbia.yaml --cache-dir data --output outputs/with_virtual.geojson --include-virtual
hydro-map build configs/columbia.yaml --cache-dir data --output outputs/two_projects.geojson --projects MICA REVELSTOKE
hydro-map build configs/columbia.yaml --source data/hybas_na_lev12_v1c/hybas_na_lev12_v1c.shp --output outputs/from_local.geojson
```

`local` subtracts the upstream catchments of selected outlet groups from each group's total upstream catchment. `total` retains the entire upstream catchment. Shared projects do not subtract each other; both receive the same explicitly marked group polygon. `--projects` filters output only: every project in the YAML remains an upstream cutoff when calculating local catchments. Adding upstream projects can therefore change existing local boundaries; rebuild geometry and weather weights after changing the selection.

Virtual endorheic connections (`ENDO=2`) are excluded by default. These links can connect internally draining basins for network representation; they do not necessarily represent physical surface-water drainage. `--include-virtual` enables them explicitly, and `--exclude-virtual` disables them. The YAML default is `dataset.include_virtual_connections`.

## Project configuration

Each project specifies a unique `id`, public `name`, `river`, country code, and an `outlet` with `hybas_id`, geographic `lon` and `lat`, a public `source`, and a description of the coordinate `reference`.

An optional unique `project_code` supplies the CSV identifier. It is retained in GeoJSON properties; the GeoJSON `id` and network relationships keep using the canonical project ID. Without `project_code`, the CSV uses `id`.

```yaml
dataset:
  region: na
  level: 12
  version: '1c'
  include_virtual_connections: false
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
```

The example uses published Global Dam Watch river-aligned points where available, plus government project locations and NOAA forecast or monitoring references. A reference point is not necessarily a surveyed dam-wall position. Its source and interpretation are retained in the configuration.

The configured point must fall within its specified HydroBASINS unit. A mismatch fails validation; points are not automatically snapped to a nearby unit. Review the reference and unit together before changing either.

Two projects may use the same unit only when both declare the same `outlet_group`. Group names must differ from project IDs, and a group cannot span different units. This is an explicit approximation, not a dam-wall delineation. Descriptive `kind` and `mw` values are configured inputs; they are not used to draw the polygons or independently audited as current plant capacities.

Catchments contain whole HydroBASINS units, including the selected outlet unit. A unit ID identifies a dataset polygon and its downstream connection, not an exact dam outlet. This method cannot split a unit at the dam wall; exact dam delineation requires finer terrain and river data. Level 12 still has finite spatial resolution.

## GeoJSON output

Output uses the standard `FeatureCollection` → `features[]` → `Feature` structure, with `properties` and `geometry` on each feature. Coordinate positions are **[longitude, latitude]** in WGS84 decimal degrees. Geometry can be `Polygon` or `MultiPolygon`; disconnected pieces are preserved. Each feature carries:

| Property | Meaning |
| --- | --- |
| `id`, `name`, `kind`, `mw` | Configured identifier, name, operational category and capacity; unknown category/capacity is null |
| `lat`, `lon` | Configured reference-point coordinates |
| `area` | Total upstream area, in km²; identical to `area_total` |
| `area_local` | Selected-project local catchment area, in km² |
| `area_total` | Total upstream catchment area, in km² |
| `area_geometry` | Area of the actual emitted geometry, in km²; equals `area_local` for a local layer |
| `up` | Direct selected upstream project IDs |
| `above` | All selected upstream project IDs |
| `down` | Nearest selected downstream project ID when unique, otherwise null |
| `down_candidates` | Downstream project IDs at the next outlet unit; multiple IDs identify a shared group |
| `part` | `local` or `total` |
| `forcing_group` | Unique catchment key for spatial aggregation and water-balance accounting |
| `geometry_status` | `hydrobasins_delineation` or `shared_unit_approximation` |
| `shared_outlet_projects` | IDs sharing this unit, or an empty list for an ordinary project |
| `hybas_id`, `hybas_ids`, `unit_count` | Selected outlet unit, emitted geometry's member units, and their count |
| `outlet_source`, `outlet_reference` | Reference-point provenance and interpretation |

Areas are calculated afresh from the corresponding total/local polygons on the WGS84 ellipsoid. They are not copied from HydroRIVERS attributes or the source dataset's rounded area fields. Relationships describe the selected outlet-unit network; they do not resolve the physical order or diversion routing inside shared groups.

**Schema version 2 changes `area` to total upstream area.** Earlier versions used `area` for the emitted geometry. Use `area_geometry`, or recompute from `geometry`, for the actual layer area. Plotting and bounding-box export already recompute geometry area, so they do not depend on a stored `area` interpretation. Metadata records `schema_version: 2` and `area_property`.

Collection metadata records the dataset version and URL, source-component SHA-256 hashes, virtual-connection setting, all local cutoffs, area method, and any repaired source geometries. Configured descriptive fields such as `river` and `country` are retained on features.

The common property contract is `id`, `name`, `kind`, `mw`, `lat`, `lon`, `area`, `area_local`, and `part`, plus `geometry` on the same GeoJSON feature. Additional topology and provenance fields are retained. `area_hydrorivers` and `divisions` require their own source data and definitions; this workflow does not substitute computed polygon area or empty objects for unknown values. Consumers should accept additional properties and collection metadata. Matching the structure does not imply identical boundaries or numeric values.

`lat` and `lon` identify the configured outlet reference point. They are distinct from the polygon centroid exported in the CSV. The feature `id` and network links retain canonical project IDs; `project_code` supplies the optional tabular identifier.

All 43 geometries and attributes are in **one GeoJSON file**. An external reference file can use the same standard FeatureCollection layout. With optional GeoPandas installed, both can be inspected identically:

```python
import geopandas as gpd

gdf = gpd.read_file("outputs/projects43/dam_catchments.geojson")
print(gdf[["id", "name", "kind", "mw", "lat", "lon", "area", "area_local", "part", "geometry"]])
print(gdf["id"].nunique())
print(gdf.iloc[0]["geometry"])
```

The final line displays WKT such as `POLYGON ((...))`; GeoJSON itself stores coordinate arrays, not a WKT string. Coordinates are always longitude first. For an external file, verify the meaning of `area` and its CRS; map comparisons calculate areas directly from the polygons.

`--table-output outputs/projects43/dam_catchments.csv` additionally writes the common attributes and WKT `geometry` in each row of a single CSV. It also includes `area_total`, `area_geometry` and shared-group fields. This is different from the bounding-box CSV below: the geometry table contains the full boundary. Plotting reads this CSV directly. For GeoPandas use `pandas.read_csv`, then `geopandas.GeoSeries.from_wkt(df.pop("geometry"), crs="EPSG:4326")` to construct its spatial column. Continue using the GeoJSON for forcing downloads.

## Bounding-box CSV

Add `--csv-output` to `build` to export the same selected geometries as a table:

```bash
hydro-map build configs/columbia.yaml --output outputs/catchments.geojson --csv-output outputs/polygon_grid_bbox.csv --bbox-buffer 0.3
```

Columns, in order: `ProjectCode`, `PolygonName`, `MinLatitude`, `MaxLatitude`, `MinLongitude`, `MaxLongitude`, `CentroidLatitude`, `CentroidLongitude`, `AreaKm2`, `BufferDegreesApplied`.

Rows follow YAML order. `PolygonName` combines the project name with `local incremental catchment` or `total upstream catchment`, according to `--part`. The CSV uses the same selected-project cutoffs and virtual-connection setting as its companion GeoJSON.

Shared-unit rows instead include `shared local catchment [GROUP]` or `shared total catchment [GROUP]` in `PolygonName`, retaining the same ten columns while making duplicated areas visible in the standalone table.

Area is recalculated on the WGS84 ellipsoid from the unbuffered geometry and written to one decimal place. Centroids use the unbuffered polygon in the longitude/latitude plane, rounded to four decimals; these coordinates are polygon centroids, not dam reference points or geodesic centers.

Only the bounding box is expanded by `--bbox-buffer` degrees on each side (default `0.3`; use `0` for no padding). Bounds are clipped to geographic coordinate limits and rounded outward to four decimals so the exported extent covers the padded geometry. Outward rounding can differ from nearest rounding by `0.0001°`. `BufferDegreesApplied` records the supplied padding without forcing one-decimal precision. Buffering does not change area or centroid.

## Compare maps

```bash
hydro-map plot one.geojson two.geojson --project MICA --labels A B --output figures/mica.png
hydro-map plot one.geojson two.geojson --project MICA --labels A B --basemap light --output figures/mica_map.png
hydro-map plot outputs/projects43/dam_catchments.geojson data/reference/catchments.csv --project MICA --labels HydroBASINS Reference --basemap terrain --output figures/mica_comparison.png
hydro-map plot outputs/projects43/dam_catchments.geojson data/reference/catchments.csv --by-project --labels HydroBASINS Reference --output-dir figures/comparison
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

Published dam-reference coordinates come from [Global Dam Watch dataset metadata](https://doi.org/10.6084/m9.figshare.25988293), [NOAA NWRFC station metadata](https://www.nwrfc.noaa.gov/river/site_meta_csv.cgi), and the government documents linked in the YAML. Retain those source links and follow the respective publishers' data licenses and attribution requirements when redistributing reference data or derived products.
