# Project outlets and shared catchments

The example configuration contains 43 project IDs and 41 distinct outlet groups.
It includes Columbia basin projects and Ross on the Skagit River. It is an
editable modeling selection, not a complete dam inventory.

## Delineation method

The default configuration uses `dataset.delineation: outlet_cell`. Each outlet
has a documented reference location and an explicit 15 arc-second HydroSHEDS
pixel center. The build traces every cell that drains to that outlet through
D8 flow directions, then converts the contributing cells to polygons.

[HydroBASINS level 12](https://www.hydrosheds.org/products/hydrobasins) provides a
search envelope and location checks. It does not determine the final boundary.
Feature `hybas_id` retains the outlet-reference index. Collection
`search_envelope` metadata retains the HydroBASINS source hashes; the raster
schema (version 3) omits `hybas_ids` and `unit_count`. Any repaired HydroBASINS
IDs are recorded under `search_envelope`; they concern the envelope only. The traversal can split the outlet
unit and excludes tributaries that enter below the cutoff. Merely clipping the outlet unit while retaining all upstream
units would not provide that guarantee.

The official [HydroSHEDS core download page](https://www.hydrosheds.org/hydrosheds-core-downloads)
provides the [North America DIR 15s](https://data.hydrosheds.org/file/hydrosheds-v1-dir/hyd_na_dir_15s.zip)
and [ACA 15s](https://data.hydrosheds.org/file/hydrosheds-v1-aca/hyd_na_aca_15s.zip)
rasters. The publisher states that 15s flow directions are unchanged between
versions 1.0 and 1.1. The [technical documentation](https://data.hydrosheds.org/file/technical-documentation/HydroSHEDS_TechDoc_v1_4.pdf)
defines the WGS84 grid, ESRI D8 directions, terminal cells, accumulation units,
and license.

`build` downloads and caches the required rasters automatically. To use local
files, provide both `--flow-direction` and `--flow-accumulation`; the latter
must be ACA in hectares, not ACC in cell counts.

Raster routing excludes virtual endorheic connections. Inland terminal cells
remain sinks; a HydroBASINS virtual link does not make that land physically
contribute to a river outlet. The raster method rejects `--include-virtual`.
For reproducing the previous whole-unit approximation, select
`dataset.delineation: outlet_unit` explicitly. Only that legacy method allows
virtual links as a network option.

## Reference locations and outlet cells

Published river-aligned points come from
[Global Dam Watch](https://doi.org/10.6084/m9.figshare.25988293). Government sources
are recorded in the YAML. A river-aligned point, a monitoring location, a dam
inventory point, and a surveyed dam-wall location are different references;
source labels must retain that distinction.

Six coarse or monitoring references were replaced with dam inventory locations
from the [USACE National Inventory of Dams public service](https://geospatial.sec.usace.army.mil/dls/rest/services/NID/National_Inventory_of_Dams_Public_Service/FeatureServer/0).
The service returns NAD83 data natively; the selected point geometries were
requested with `outSR=4326` for WGS84 coordinates.

| Project | NID identifier | WGS84 longitude | WGS84 latitude |
| --- | --- | ---: | ---: |
| Cabinet Gorge | `ID00222` | -116.0572528918 | 48.0856843234 |
| Wells | `WA00098` | -119.8783145651 | 47.9483059040 |
| Little Goose | `WA00331` | -118.0269139236 | 46.5842460112 |
| Lower Monumental | `WA00270` | -118.5377043260 | 46.5621950661 |
| Rocky Reach | `WA00086` | -120.2955045384 | 47.5334488340 |
| Chief Joseph | `WA00299` | -119.6374416783 | 47.9953649940 |

Corra Linn uses the [BC Geographical Names official approximate dam centre](https://apps.gov.bc.ca/pub/bcgnws/names/51921.html):
49°27′59″ N, 117°28′00″ W, explicitly recorded as WGS84. Its containing
HydroSHEDS river cell is centered at 49.4645833333° N, 117.4687500000° W,
approximately 251 m away. The published dam location selects this cell;
no drainage-area target is used. Kootenay Canal retains the same dam-catchment
proxy for its headpond intake. This does not delineate or allocate runoff
along the separate diversion reach.

For the six NID locations, the chosen raster cell is the nearest reviewed main-river
cell within the configured HydroBASINS unit. The remaining GDW and NOAA references retain their
published river-aligned cells. Each choice is explicit in `grid_lon`,
`grid_lat`, and `grid_reference`; reported drainage areas are not used to fit
or select an outlet. Downstream cell traces were checked against the selected
project order and named rivers.

The Wells inventory point is approximately 829 m from its chosen river cell.
This is larger than the other reference-to-cell distances and remains a spatial
uncertainty for finer-scale work. More precise dam-wall and river alignment
should be checked before treating that cutoff as surveyed.

The resulting boundaries are derived grid catchments. Their publishers have
not certified them as dam-specific boundaries. A finer grid can improve outlet
placement but does not by itself resolve diversions, reservoir operations, or
incorrect reference locations.

## Shared outlet groups

| Projects | HYBAS_ID | Common cutoff |
| --- | --- | --- |
| Corra Linn and Kootenay Canal | `7120295800` | Corra Linn dam cell, used as the headpond intake proxy |
| Seven Mile and Waneta | `7120305410` | Waneta, downstream of Seven Mile |

The configuration retains these two shared groups as explicit spatial proxies.
Without a declared group, the raster method can keep distinct cells in the
same HydroBASINS unit as independent outlets.
Within a group, all member cells must lie on one downstream path; the
most-downstream cell defines the common catchment. Both records receive the
same polygon and `geometry_status: shared_unit_approximation`. Independent
projects use `outlet_cell_delineation`.

The projects remain separate records, but their shared geometries and weather
averages must not be counted twice in a water balance. Deduplicate by
`forcing_group`. Sharing is a modeling choice: Seven Mile and Waneta have
distinct configured raster cells and a resolved downstream order, but still
receive the agreed group polygon. Separate local inflows require a revised
group definition and routing model.

Kootenay Canal diverts water from the Corra Linn headpond and returns it to the
river at South Slocan. The configuration therefore uses the Corra Linn dam catchment as a shared
headpond intake proxy, rather than the canal powerhouse location. The topographic catchment does not determine
how water is allocated between facilities.
[BC Hydro describes the diversion](https://www.bchydro.com/community/recreation_areas/kootenay_canal.html).

Seven Mile retains the [NOAA SEVQ2 reference](https://www.nwrfc.noaa.gov/river/station/flowplot/flowplot.cgi?SEVQ2=)
and its own documented outlet cell. Waneta supplies the group's downstream
cutoff; the two individual cell coordinates remain available for audit.

## Area and topology

A total catchment contains all cells draining to the group's cutoff. A local
catchment subtracts the total catchments of selected upstream groups. Shared
projects form one cutoff group; neither member subtracts the other. Adding
upstream projects can therefore change existing local boundaries even when
outlet coordinates stay the same.

Areas are calculated from the resulting polygons on the WGS84 ellipsoid.
`area` and `area_total` contain total upstream group area; `area_local` contains
local group area; and `area_geometry` contains the area of the emitted geometry.
All four are in km². With `part: local`, use `area_geometry` or `area_local` for
that polygon's water balance, after deduplicating shared groups.

The native HydroSHEDS ACA layer stores upstream area in hectares using the
publisher's cell-area weights. Divide by 100 to express it in km². That value
and an ellipsoidal area recalculated from the same cell boundary may differ
slightly. Native ACA is an independent diagnostic; it does not replace or rescale
the geometry area.

Compare areas only when outlet definition, upstream cutoffs, routing policy,
geometry extent, and area method agree. A plausible area alone does not verify
a boundary. Configured plant capacities and operational classifications are
descriptive inputs and have not been independently audited.
