# hydro-map

Build selected-project catchments from HydroBASINS topology and compare polygon layers on maps.

## Quick start

Run from this directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[maps]'
hydro-map download --config configs/columbia.yaml --cache-dir data
hydro-map build configs/columbia.yaml --cache-dir data --output outputs/catchments.geojson --csv-output outputs/polygon_grid_bbox.csv
hydro-map plot outputs/catchments.geojson --project MICA --labels HydroBASINS --basemap terrain --output figures/mica.png
```

The example selects 26 Columbia River basin projects. It is an editable selection, not a complete dam inventory. Change the YAML project list to define the upstream cutoffs for your application.

## Build options

```bash
hydro-map build configs/columbia.yaml --cache-dir data --output outputs/total.geojson --part total
hydro-map build configs/columbia.yaml --cache-dir data --output outputs/local.geojson --part local --exclude-virtual
hydro-map build configs/columbia.yaml --cache-dir data --output outputs/with_virtual.geojson --include-virtual
hydro-map build configs/columbia.yaml --cache-dir data --output outputs/two_projects.geojson --projects MICA REVELSTOKE
hydro-map build configs/columbia.yaml --source data/hybas_na_lev12_v1c/hybas_na_lev12_v1c.shp --output outputs/from_local.geojson
```

`local` subtracts the upstream catchments of selected projects from each selected project's total upstream catchment. `total` retains the entire upstream catchment. `--projects` filters output only: every project in the YAML remains an upstream cutoff when calculating local catchments.

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

Catchments contain whole HydroBASINS units, including the selected outlet unit. A unit ID identifies a dataset polygon and its downstream connection, not an exact dam outlet. This method cannot split a unit at the dam wall; exact dam delineation requires finer terrain and river data. Level 12 still has finite spatial resolution.

## GeoJSON output

Output uses the standard `FeatureCollection` → `features[]` → `Feature` structure, with `properties` and `geometry` on each feature. Coordinate positions are **[longitude, latitude]** in WGS84 decimal degrees. Geometry can be `Polygon` or `MultiPolygon`; disconnected pieces are preserved. Each feature carries:

| Property | Meaning |
| --- | --- |
| `id`, `name` | Configured project identifier and name |
| `lat`, `lon` | Configured reference-point coordinates |
| `area` | Area of the emitted geometry, in km² |
| `area_local` | Selected-project local catchment area, in km² |
| `area_total` | Total upstream catchment area, in km² |
| `up` | Direct selected upstream project IDs |
| `above` | All selected upstream project IDs |
| `down` | Nearest selected downstream project ID, or null |
| `part` | `local` or `total` |
| `hybas_id`, `hybas_ids`, `unit_count` | Selected outlet unit, emitted geometry's member units, and their count |
| `outlet_source`, `outlet_reference` | Reference-point provenance and interpretation |

Areas are calculated afresh from the output polygons on the WGS84 ellipsoid. They are not copied from HydroRIVERS attributes or the source dataset's rounded area fields. Relationships describe the selected project network, not every dam or stream junction.

Collection metadata records the dataset version and URL, source-component SHA-256 hashes, virtual-connection setting, all local cutoffs, area method, and any repaired source geometries. Configured descriptive fields such as `river` and `country` are retained on features.

The shared property contract is `id`, `name`, `lat`, `lon`, `area`, `area_local`, `up`, `above`, `down`, and `part`. Descriptive fields such as `owner`, `kind`, and `note` pass through when configured. `area_hydrorivers` and `divisions` require their own source data and definitions; this workflow does not substitute computed polygon area or empty objects for unknown values. Consumers should accept additional properties and collection metadata. Matching the structure does not imply identical boundaries or numeric values.

`lat` and `lon` identify the configured outlet reference point. They are distinct from the polygon centroid exported in the CSV. The feature `id` and network links retain canonical project IDs; `project_code` supplies the optional tabular identifier.

## Bounding-box CSV

Add `--csv-output` to `build` to export the same selected geometries as a table:

```bash
hydro-map build configs/columbia.yaml --output outputs/catchments.geojson --csv-output outputs/polygon_grid_bbox.csv --bbox-buffer 0.3
```

Columns, in order: `ProjectCode`, `PolygonName`, `MinLatitude`, `MaxLatitude`, `MinLongitude`, `MaxLongitude`, `CentroidLatitude`, `CentroidLongitude`, `AreaKm2`, `BufferDegreesApplied`.

Rows follow YAML order. `PolygonName` combines the project name with `local incremental catchment` or `total upstream catchment`, according to `--part`. The CSV uses the same selected-project cutoffs and virtual-connection setting as its companion GeoJSON.

Area is recalculated on the WGS84 ellipsoid from the unbuffered geometry and written to one decimal place. Centroids use the unbuffered polygon in the longitude/latitude plane, rounded to four decimals; these coordinates are polygon centroids, not dam reference points or geodesic centers.

Only the bounding box is expanded by `--bbox-buffer` degrees on each side (default `0.3`; use `0` for no padding). Bounds are clipped to geographic coordinate limits and rounded outward to four decimals so the exported extent covers the padded geometry. Outward rounding can differ from nearest rounding by `0.0001°`. `BufferDegreesApplied` records the supplied padding without forcing one-decimal precision. Buffering does not change area or centroid.

## Compare maps

```bash
hydro-map plot one.geojson two.geojson --project MICA --labels A B --output figures/mica.png
hydro-map plot one.geojson two.geojson --project MICA --labels A B --basemap light --output figures/mica_map.png
hydro-map plot outputs/catchments.geojson --project MICA --basemap terrain --coordinates lonlat --output figures/mica_terrain.png
hydro-map plot outputs/catchments.geojson --project MICA --coordinates projected --output figures/mica_projected.pdf
hydro-map plot outputs/catchments.geojson --by-project --output-dir figures
```

Plotting also accepts shapefiles with their accompanying `.prj` and component files. Use matching project identifiers to compare the same project across layers.

Each title shows the project name and each layer's area recalculated on the WGS84 ellipsoid. Layers use distinct outlines and light translucent fills, with a local geodesic scale bar, north arrow, and dam-reference markers. Stored `area` properties do not affect the calculation. `--title` changes the heading; `--id-field` and `--name-field` select alternative attribute names. Save as PNG, SVG, or PDF by choosing the output extension.

`--coordinates lonlat` is the default: longitude and latitude axes have degree and hemisphere labels, with aspect corrected at the map's central latitude. `--coordinates projected` uses a local Lambert azimuthal equal-area projection and shows kilometre ticks. Display projection never changes the input GeoJSON or the ellipsoidal area calculation.

`--basemap terrain` adds Esri World Topographic Map tiles; `--basemap light` adds Esri World Light Gray Canvas tiles. Install the optional mapping dependencies with `pip install -e '.[maps]'`. Tiles are reprojected to the plot CRS, cached under `data/map_tiles` (change with `--tile-cache`), and attributed inside the map's lower-right corner. The first request for an uncached tile needs network access. Keep publisher attribution on shared figures and follow the provider's usage terms. Background maps provide visual context; they do not define catchment boundaries.

The default `--basemap none` needs no tile service or mapping extra. Use `pip install -e .` for this offline plotting setup. A failed basemap request reports an error; retry it or choose `--basemap none` explicitly. Details of tile reprojection are in the [contextily documentation](https://contextily.readthedocs.io/en/latest/warping_guide.html).

Plot inputs must contain valid Polygon or MultiPolygon geometries. GeoJSON coordinates must be WGS84; shapefiles are reprojected from their `.prj`. Batch mode requires the same project IDs across all inputs and writes one PNG per project plus an overview. It refuses to overwrite existing project figures; use a new output directory for another comparison.

## Tests

```bash
python -m unittest discover -s tests
```

Tests use small local fixtures and do not require downloading the regional dataset. Downloaded data, generated boundaries, and figures are excluded from version control.

## Data attribution and terms

HydroBASINS is published through [HydroSHEDS](https://www.hydrosheds.org/products/hydrobasins). The example downloads the [North America standard level 12 version 1c archive](https://data.hydrosheds.org/file/hydrobasins/standard/hybas_na_lev12_v1c.zip). Attribute HydroBASINS and cite Lehner and Grill (2013), [Global river hydrography and network routing](https://doi.org/10.1002/hyp.9740). HydroBASINS uses the HydroSHEDS core-product license: consult the product page's linked license agreement and [publisher terms](https://www.hydrosheds.org/terms-of-use) for attribution, redistribution, and use conditions.

Published dam-reference coordinates come from [Global Dam Watch dataset metadata](https://doi.org/10.6084/m9.figshare.25988293), [NOAA NWRFC station metadata](https://www.nwrfc.noaa.gov/river/site_meta_csv.cgi), and the government documents linked in the YAML. Retain those source links and follow the respective publishers' data licenses and attribution requirements when redistributing reference data or derived products.
