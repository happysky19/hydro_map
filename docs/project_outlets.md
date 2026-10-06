# Project outlets and shared catchments

The example configuration contains 43 project IDs and 41 distinct HydroBASINS
outlet groups. It includes Columbia basin projects and Ross on the Skagit River.

Boundaries come from the published [HydroBASINS North America standard level 12,
version 1c archive](https://data.hydrosheds.org/file/hydrobasins/standard/hybas_na_lev12_v1c.zip).
The build follows its downstream network and combines whole subbasins. Dam
reference points come from [Global Dam Watch](https://doi.org/10.6084/m9.figshare.25988293)
and the government sources recorded in the YAML. The resulting project
catchments are derived polygons; the publishers have not certified them as
dam-specific boundaries.

HydroBASINS level 12 has finite resolution. Its subbasins were derived from
15 arc-second HydroSHEDS grids, with splits at qualifying river confluences.
A dam can lie inside a subbasin, so selecting its containing unit can include
land draining below the dam. Two dams can also occupy one unit. See the
[HydroBASINS product description](https://www.hydrosheds.org/products/hydrobasins)
and [technical documentation](https://data.hydrosheds.org/file/technical-documentation/HydroBASINS_TechDoc_v1c.pdf).

## Shared outlet groups

| Projects | HYBAS_ID | Interpretation |
| --- | --- | --- |
| Corra Linn and Kootenay Canal | `7120295800` | Shared headpond and unresolved local subdivision |
| Seven Mile and Waneta | `7120305410` | Two successive dams inside one HydroBASINS unit |

Each pair receives the same group catchment as an explicit spatial proxy. The
projects remain separate records, but their geometries and weather averages
must not be counted twice in a basin water balance. Independent local inflows
require finer outlet delineation and a separate routing model.

Kootenay Canal diverts water from the Corra Linn headpond and returns it to the
river at South Slocan. Its intake catchment and flow allocation therefore depend
on the diversion system. The configuration uses the Corra Linn reference point
for that shared intake group. [BC Hydro describes the diversion](https://www.bchydro.com/community/recreation_areas/kootenay_canal.html).

Seven Mile uses the [NOAA SEVQ2 dam reference](https://www.nwrfc.noaa.gov/river/station/flowplot/flowplot.cgi?SEVQ2=):
49.029722222° N, 117.503055556° W. This point and the Waneta reference both fall
inside `7120305410`; changing the reference point does not split that polygon.

## Area and topology

A total catchment contains the outlet group and all connected upstream units.
A local catchment removes the total catchments of selected upstream groups.
Shared projects form one cutoff group; neither member subtracts the other.
Adding upstream projects can therefore change existing local boundaries even
when the outlet coordinates stay the same. Internally draining areas connected
only by virtual links are excluded by default.

Areas are calculated from the resulting polygons on the WGS84 ellipsoid.
In schema version 2, `area` and `area_total` contain total upstream area;
`area_local` contains the selected-project local area; and `area_geometry`
contains the area of the emitted `geometry`. With `part: local`, use
`area_geometry` or `area_local` for a polygon water balance. Deduplicate shared
records by `forcing_group` before adding their areas.

Compare areas only when outlet definition, upstream cutoffs, virtual-connection
policy, and geometry extent agree. Configured plant capacities and operational
classifications are descriptive inputs and have not been independently audited.
