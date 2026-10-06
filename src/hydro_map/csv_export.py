"""Export catchment summaries and buffered geographic download extents."""

import csv
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import math
import json
from pathlib import Path

from shapely.geometry import shape

from .basins import area_km2


COLUMNS = ["ProjectCode", "PolygonName", "MinLatitude", "MaxLatitude",
           "MinLongitude", "MaxLongitude", "CentroidLatitude", "CentroidLongitude",
           "AreaKm2", "BufferDegreesApplied"]


def write_geometry_csv(collection: dict, output: Path) -> int:
    """Keep project attributes and WGS84 geometry in one CSV; geometry uses WKT."""
    columns = ['id', 'name', 'kind', 'mw', 'lat', 'lon', 'area', 'area_local', 'part',
               'geometry', 'area_total', 'area_geometry', 'forcing_group',
               'geometry_status', 'shared_outlet_projects', 'catchment_role',
               'diversion_intake_project', 'routing_requires_operations']
    rows = []
    for feature in collection['features']:
        geometry = shape(feature['geometry'])
        if geometry.is_empty or not geometry.is_valid or geometry.geom_type not in {'Polygon', 'MultiPolygon'}:
            raise ValueError('Geometry table requires valid polygons')
        props = feature['properties']
        row = {key: props.get(key) for key in columns if key != 'geometry'}
        row['geometry'] = geometry.wkt
        row['shared_outlet_projects'] = json.dumps(props.get('shared_outlet_projects', []))
        if 'routing_requires_operations' in props:
            row['routing_requires_operations'] = json.dumps(props['routing_requires_operations'])
        rows.append(row)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def write_bbox_csv(collection: dict, output: Path, buffer_degrees: float = 0.3) -> int:
    """Calculate rows from unbuffered WGS84 geometry; pad only the bounding box."""
    if not math.isfinite(buffer_degrees) or buffer_degrees < 0:
        raise ValueError("Bounding-box buffer must be finite and nonnegative")
    pad = Decimal(str(buffer_degrees))
    precision = Decimal("0.0001")
    rows = []
    for feature in collection["features"]:
        props = feature["properties"]
        geometry = shape(feature["geometry"])
        if geometry.is_empty or not geometry.is_valid or geometry.geom_type not in {"Polygon", "MultiPolygon"}:
            raise ValueError(f"Invalid polygon for {props['id']}")
        west, south, east, north = [Decimal(str(value)) for value in geometry.bounds]
        if not (-180 <= west <= east <= 180 and -90 <= south <= north <= 90):
            raise ValueError(f"Coordinates outside WGS84 bounds for {props['id']}")
        part = props["part"]
        if part not in {"local", "total"}:
            raise ValueError(f"Unknown catchment part: {part}")
        suffix = "local incremental catchment" if part == "local" else "total upstream catchment"
        if props.get('geometry_status') == 'shared_unit_approximation':
            suffix = f"shared {part} catchment [{props['forcing_group']}]"
        if props.get('catchment_role') == 'natural_reach_at_tailrace':
            suffix = f'{part} natural reach catchment at tailrace'
        centroid = geometry.centroid
        # Round limits outward so displayed precision never reduces the padded extent.
        bounds = [(max(Decimal(-90), south - pad), ROUND_FLOOR),
                  (min(Decimal(90), north + pad), ROUND_CEILING),
                  (max(Decimal(-180), west - pad), ROUND_FLOOR),
                  (min(Decimal(180), east + pad), ROUND_CEILING)]
        rows.append([props.get("project_code", props["id"]), f"{props['name']} {suffix}",
                     *[format(value.quantize(precision, rounding=mode), ".4f") for value, mode in bounds],
                     f"{centroid.y:.4f}", f"{centroid.x:.4f}", f"{area_km2(geometry):.1f}",
                     str(buffer_degrees)])
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(COLUMNS)
        writer.writerows(rows)
    return len(rows)
