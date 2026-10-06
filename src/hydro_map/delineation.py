"""Resolve project catchments from physical HydroSHEDS flow directions."""

from collections import defaultdict
from dataclasses import replace
import math
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window, from_bounds
from shapely.geometry import mapping, shape

from .basins import GEOD, _sha256, area_km2, build_catchments
from .flow import mask_geometry, upstream_mask


def build_cell_catchments(config, source, part, selected, flow_direction, flow_accumulation):
    if config.dataset.include_virtual_connections:
        raise ValueError('Outlet-cell delineation cannot include virtual connections')
    if flow_direction is None or flow_accumulation is None:
        raise ValueError('Outlet-cell delineation requires flow direction and accumulation rasters')
    # Whole-unit totals bound the search; only cell reachability determines membership.
    coarse = build_catchments(replace(config, dataset=replace(
        config.dataset, delineation='outlet_unit')), source, part='total')
    bounds = np.array([shape(f['geometry']).bounds for f in coarse['features']])
    west, south = bounds[:, :2].min(axis=0)
    east, north = bounds[:, 2:].max(axis=0)
    points, accumulation, distances, cells = {}, {}, {}, {}
    with rasterio.open(flow_direction) as direction, rasterio.open(flow_accumulation) as area:
        if (direction.crs != rasterio.crs.CRS.from_epsg(4326)
                or area.crs != direction.crs or area.transform != direction.transform
                or area.shape != direction.shape or direction.count != 1 or area.count != 1):
            raise ValueError('Flow rasters must be aligned single-band WGS84 grids')
        t = direction.transform
        if (not math.isclose(t.a, 1 / 240, abs_tol=1e-12)
                or not math.isclose(t.e, -1 / 240, abs_tol=1e-12)
                or t.b != 0 or t.d != 0 or direction.nodata != 255
                or area.nodata != 4294967295):
            raise ValueError('Expected native HydroSHEDS 15-arcsecond direction and hectare accumulation grids')
        crop = from_bounds(west, south, east, north, t)
        col0 = max(0, math.floor(crop.col_off) - 8)
        row0 = max(0, math.floor(crop.row_off) - 8)
        col1 = min(direction.width, math.ceil(crop.col_off + crop.width) + 8)
        row1 = min(direction.height, math.ceil(crop.row_off + crop.height) + 8)
        if col0 >= col1 or row0 >= row1:
            raise ValueError('Search envelope is outside the flow grid')
        window = Window(col0, row0, col1 - col0, row1 - row0)
        flow = direction.read(1, window=window)
        transform = direction.window_transform(window)
        for project in config.projects:
            if project.grid_lon is None or project.grid_lat is None or not project.grid_reference:
                raise ValueError(f'{project.id}: a documented outlet grid cell is required')
            row, col = direction.index(project.grid_lon, project.grid_lat)
            if not (0 <= row < direction.height and 0 <= col < direction.width):
                raise ValueError(f'{project.id}: outlet cell is outside the source grid')
            lon, lat = direction.xy(row, col)
            if max(abs(lon - project.grid_lon), abs(lat - project.grid_lat)) > 1e-7:
                raise ValueError(f'{project.id}: grid coordinates must identify a native cell center')
            distance = GEOD.inv(project.lon, project.lat, lon, lat)[2]
            if distance > 1000:
                raise ValueError(f'{project.id}: outlet cell is more than 1000 m from the reference point')
            value = area.read(1, window=Window(col, row, 1, 1))[0, 0]
            if value == area.nodata or value <= 0:
                raise ValueError(f'{project.id}: missing upstream accumulation at outlet cell')
            points[project.id] = (row - row0, col - col0)
            cells[project.id] = {'row': row, 'col': col, 'lon': lon, 'lat': lat}
            accumulation[project.id] = float(value) / 100
            distances[project.id] = distance

    masks = {point: upstream_mask(flow, point) for point in set(points.values())}
    groups = defaultdict(list)
    for project in config.projects:
        groups[project.metadata.get('outlet_group', project.id)].append(project)
    outlets, totals = {}, {}
    for group, members in groups.items():
        if any(not (masks[points[p.id]][points[q.id]]
                    or masks[points[q.id]][points[p.id]])
               for index, p in enumerate(members) for q in members[index + 1:]):
            raise ValueError(f'{group}: shared outlets are not connected along one flow path')
        candidates = [p for p in members if all(
            masks[points[p.id]][points[q.id]] for q in members)]
        if not candidates:
            raise ValueError(f'{group}: shared outlets are not connected along one flow path')
        outlet = candidates[0]
        outlets[group] = outlet
        totals[group] = masks[points[outlet.id]]
    if len({points[p.id] for p in outlets.values()}) != len(outlets):
        raise ValueError('Different forcing groups select the same outlet cell; declare a shared outlet_group')
    counts = {group: int(mask.sum()) for group, mask in totals.items()}
    above, downstream = {}, {}
    for group, mask in totals.items():
        above[group] = [g for g, outlet in outlets.items()
                        if g != group and mask[points[outlet.id]]]
        below = [g for g in groups if g != group and
                 totals[g][points[outlets[group].id]]]
        downstream[group] = min(below, key=counts.get) if below else None

    project_groups = {p.id: group for group, members in groups.items() for p in members}
    for project in config.projects:
        intake = project.metadata.get('diversion_intake_project')
        if intake is not None and project_groups.get(intake) not in above[project_groups[project.id]]:
            raise ValueError(f'{project.id}: diversion intake must be upstream of the natural return-point outlet')

    features = []
    for group, members in groups.items():
        if not any(p.id.casefold() in selected for p in members):
            continue
        total = totals[group]
        local = total.copy()
        for upstream_group in above[group]:
            local &= ~totals[upstream_group]
        if not local.any():
            raise ValueError(f'{group}: local catchment is empty')
        total_geometry = mask_geometry(total, transform)
        local_geometry = mask_geometry(local, transform)
        total_area, local_area = area_km2(total_geometry), area_km2(local_geometry)
        chosen = local_geometry if part == 'local' else total_geometry
        upstream_projects = [p.id for g in above[group] for p in groups[g]]
        immediate = [p.id for g in above[group] if downstream[g] == group for p in groups[g]]
        below = groups[downstream[group]] if downstream[group] is not None else []
        next_ids = [p.id for p in below]
        forcing_outlet = outlets[group]
        for project in members:
            if project.id.casefold() not in selected:
                continue
            properties = {
                'kind': None, 'mw': None, **project.metadata,
                'id': project.id, 'name': project.name, 'lat': project.lat, 'lon': project.lon,
                'area': total_area, 'area_total': total_area, 'area_local': local_area,
                'area_geometry': local_area if part == 'local' else total_area, 'part': part,
                'up': immediate, 'above': upstream_projects,
                'down': next_ids[0] if len(next_ids) == 1 else None, 'down_candidates': next_ids,
                'forcing_group': group,
                'geometry_status': 'shared_unit_approximation' if len(members) > 1 else 'outlet_cell_delineation',
                'shared_outlet_projects': [p.id for p in members] if len(members) > 1 else [],
                'hybas_id': project.hybas_id,
                'outlet_source': project.source, 'outlet_reference': project.reference,
                'outlet_grid': cells[project.id], 'outlet_grid_reference': project.grid_reference,
                'outlet_grid_distance_m': distances[project.id],
                'outlet_grid_upstream_area_km2': accumulation[project.id],
                'forcing_outlet_project': forcing_outlet.id,
                'forcing_outlet_grid': cells[forcing_outlet.id],
                'forcing_outlet_grid_upstream_area_km2': accumulation[forcing_outlet.id],
                'cell_count_total': counts[group], 'cell_count_local': int(local.sum()),
            }
            features.append({'type': 'Feature', 'id': project.id, 'properties': properties,
                             'geometry': mapping(chosen)})
    order = {p.id: i for i, p in enumerate(config.projects)}
    features.sort(key=lambda feature: order[feature['id']])
    return {'type': 'FeatureCollection', 'metadata': {
        'schema_version': 3, 'dataset': 'HydroSHEDS core 15 arcseconds',
        'region': config.dataset.region, 'area_units': 'km2',
        'area_property': 'total upstream catchment at the forcing-group outlet',
        'area_method': 'WGS84 ellipsoidal polygon area',
        'source_accumulation_units': 'hectares, converted to km2; independent source area convention',
        'delineation': 'All D8 cells reaching the documented outlet cell; selected upstream totals removed for local catchments',
        'grid_resolution_arcseconds': 15, 'include_virtual_connections': False,
        'network_resolution': 'Natural D8 drainage between outlet cells; engineered flow allocation is separate',
        'local_cutoffs': [p.id for p in config.projects], 'forcing_group_count': len(groups),
        'shared_outlet_groups': {g: [p.id for p in members] for g, members in groups.items() if len(members) > 1},
        'shared_group_rule': 'Use the downstream-most selected member cell; duplicate group geometry is not additive',
        'geometry_validation': 'All output geometries are nonempty valid polygons',
        'search_envelope': {'dataset': 'HydroBASINS', 'level': config.dataset.level,
                            'source_sha256': coarse['metadata']['source_sha256'],
                            'repaired_hybas_ids': coarse['metadata']['repaired_hybas_ids'],
                            'role': 'Conservative search bounds only; not catchment membership'},
        'source_urls': {layer: f'https://data.hydrosheds.org/file/hydrosheds-v1-{layer}/hyd_{config.dataset.region}_{layer}_15s.zip'
                        for layer in ('dir', 'aca')},
        'source_sha256': {'direction': _sha256(Path(flow_direction)),
                          'accumulation': _sha256(Path(flow_accumulation))},
    }, 'features': features}
