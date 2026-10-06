"""Aggregate HydroBASINS units using the published downstream network."""

from collections import defaultdict
import hashlib
from pathlib import Path

from pyproj import CRS, Geod
import shapefile
import shapely
from shapely.geometry import Point, mapping, shape

from .config import Config


GEOD = Geod(ellps="WGS84")


def area_km2(geometry) -> float:
    return abs(GEOD.geometry_area_perimeter(shapely.orient_polygons(geometry))[0]) / 1e6


def _polygonal(geometry):
    if geometry.is_empty:
        raise ValueError("Empty basin geometry")
    if not geometry.is_valid:
        geometry = shapely.make_valid(geometry)
    if geometry.geom_type == "GeometryCollection":
        polygons = [part for part in geometry.geoms if part.geom_type in {"Polygon", "MultiPolygon"}]
        geometry = shapely.union_all(polygons)
    if geometry.is_empty or geometry.geom_type not in {"Polygon", "MultiPolygon"} or not geometry.is_valid:
        raise ValueError("Basin geometry cannot be represented as valid polygons")
    return geometry


def _check_graph(downstream):
    finished = set()
    for start in downstream:
        current, active = start, set()
        while current and current not in finished:
            if current not in downstream:
                raise ValueError(f"Missing downstream HydroBASINS unit {current}; use a complete regional dataset")
            if current in active:
                raise ValueError(f"Downstream cycle at HydroBASINS unit {current}")
            active.add(current)
            current = downstream[current]
        finished.update(active)


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_catchments(config: Config, source: Path, part="local", project_ids=None) -> dict:
    """Build local or total catchments; output filtering never changes cutoffs."""
    source = Path(source)
    if part not in {"local", "total"}:
        raise ValueError("part must be local or total")
    selected = {project.id.casefold() for project in config.projects}
    if project_ids is not None:
        requested = {value.casefold() for value in project_ids}
        unknown = requested - selected
        if unknown:
            raise ValueError(f"Unknown project IDs: {', '.join(sorted(unknown))}")
        if not requested:
            raise ValueError("Select at least one project")
        selected = requested
    prj = source.with_suffix(".prj")
    if not prj.exists() or not CRS.from_wkt(prj.read_text()).equals(CRS.from_epsg(4326), ignore_axis_order=True):
        raise ValueError("HydroBASINS input must include a WGS84 .prj file")

    with shapefile.Reader(str(source)) as reader:
        fields = {field[0] for field in reader.fields[1:]}
        if not {"HYBAS_ID", "NEXT_DOWN", "ENDO"} <= fields:
            raise ValueError("Input is missing HYBAS_ID, NEXT_DOWN or ENDO")
        records, downstream, reverse = {}, {}, defaultdict(list)
        for record in reader.iterRecords():
            unit = int(record["HYBAS_ID"])
            if unit in records:
                raise ValueError(f"Duplicate HydroBASINS unit {unit}")
            records[unit] = record.oid
            target = int(record["NEXT_DOWN"])
            if int(record["ENDO"]) == 2 and not config.dataset.include_virtual_connections:
                target = 0
            downstream[unit] = target
            if target:
                reverse[target].append(unit)
        _check_graph(downstream)
        geometries, repaired = {}, set()

        def geometry(unit):
            if unit not in geometries:
                original = shape(reader.shape(records[unit]).__geo_interface__)
                if not original.is_valid:
                    repaired.add(unit)
                geometries[unit] = _polygonal(original)
            return geometries[unit]

        def upstream(unit):
            members, stack = set(), [unit]
            while stack:
                current = stack.pop()
                members.add(current)
                stack.extend(reverse[current])
            return members

        totals = {}
        for project in config.projects:
            if project.hybas_id not in records:
                raise ValueError(f"{project.id}: HydroBASINS unit {project.hybas_id} does not exist")
            if not geometry(project.hybas_id).covers(Point(project.lon, project.lat)):
                raise ValueError(f"{project.id}: outlet reference point is outside HydroBASINS unit {project.hybas_id}")
            totals[project.id] = upstream(project.hybas_id)

        project_by_unit = defaultdict(list)
        for project in config.projects:
            project_by_unit[project.hybas_id].append(project.id)
        next_project = {}
        for project in config.projects:
            current = downstream[project.hybas_id]
            while current and current not in project_by_unit:
                current = downstream[current]
            next_project[project.id] = project_by_unit.get(current, [])

        features = []
        for project in config.projects:
            if project.id.casefold() not in selected:
                continue
            members = totals[project.id]
            above = [p.id for p in config.projects
                     if p.hybas_id != project.hybas_id and p.hybas_id in members]
            removed = set().union(*(totals[value] for value in above))
            local = members - removed
            total_geometry = _polygonal(shapely.union_all([geometry(unit) for unit in sorted(members)]))
            local_geometry = _polygonal(shapely.union_all([geometry(unit) for unit in sorted(local)]))
            chosen = local_geometry if part == "local" else total_geometry
            chosen_ids = local if part == "local" else members
            shared = project_by_unit[project.hybas_id]
            downstream_projects = next_project[project.id]
            properties = {
                "kind": None, "mw": None,
                **project.metadata,
                "id": project.id, "name": project.name, "lat": project.lat, "lon": project.lon,
                "area": area_km2(total_geometry), "area_local": area_km2(local_geometry),
                "area_total": area_km2(total_geometry),
                "area_geometry": area_km2(chosen),
                "up": [value for value in above if project.id in next_project[value]],
                "above": above,
                "down": downstream_projects[0] if len(downstream_projects) == 1 else None,
                "down_candidates": downstream_projects, "part": part,
                "forcing_group": project.metadata.get("outlet_group", project.id),
                "geometry_status": "shared_unit_approximation" if len(shared) > 1 else "hydrobasins_delineation",
                "shared_outlet_projects": shared if len(shared) > 1 else [],
                "hybas_id": project.hybas_id, "hybas_ids": sorted(chosen_ids),
                "unit_count": len(chosen_ids),
                "outlet_source": project.source, "outlet_reference": project.reference,
            }
            features.append({"type": "Feature", "id": project.id, "properties": properties,
                             "geometry": mapping(shapely.orient_polygons(chosen))})

    stem = f"hybas_{config.dataset.region}_lev{config.dataset.level:02d}_v{config.dataset.version}"
    return {
        "type": "FeatureCollection",
        "metadata": {
            "schema_version": 2, "area_property": "total upstream catchment",
            "dataset": "HydroBASINS standard", "region": config.dataset.region,
            "level": config.dataset.level, "version": config.dataset.version,
            "source_url": f"https://data.hydrosheds.org/file/hydrobasins/standard/{stem}.zip",
            "include_virtual_connections": config.dataset.include_virtual_connections,
            "area_units": "km2", "area_method": "WGS84 ellipsoidal polygon area",
            "delineation": "Whole outlet subbasin plus upstream units; no dam-wall split",
            "local_cutoffs": [project.id for project in config.projects],
            "forcing_group_count": len(project_by_unit),
            "shared_outlet_groups": {p.metadata["outlet_group"]: project_by_unit[p.hybas_id]
                                     for p in config.projects if "outlet_group" in p.metadata},
            "network_resolution": "HydroBASINS outlet units; within-unit project order is unresolved",
            "repaired_geometry_count": len(repaired), "repaired_hybas_ids": sorted(repaired),
            "source_sha256": {suffix: _sha256(source.with_suffix(suffix))
                              for suffix in (".shp", ".shx", ".dbf", ".prj")},
        },
        "features": features,
    }
