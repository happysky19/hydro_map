"""Read the catchment GeoJSON and validate its routing and forcing-group metadata."""

import json
from pathlib import Path

from shapely.geometry import shape


def read_catchments(path):
    """Features of a WGS84 GeoJSON FeatureCollection, each geometry as a shapely polygon.

    Every feature needs a unique properties.id and properties.part, local or total, the
    same for all features.
    """
    doc = json.loads(Path(path).read_text())
    if doc.get('type') != 'FeatureCollection' or doc.get('crs'):
        raise ValueError('Use a WGS84 GeoJSON FeatureCollection')
    features, parts = {}, set()
    for feature in doc['features']:
        props = dict(feature.get('properties') or {})
        identifier = props.get('id')
        if not isinstance(identifier, str) or not identifier or identifier.strip() != identifier:
            raise ValueError('GeoJSON requires nonempty string properties.id values')
        if identifier in features:
            raise ValueError(f'Duplicate project identifier: {identifier}')
        if props.get('part') not in ('local', 'total'):
            raise ValueError(f'{identifier}: declare properties.part as local or total')
        geometry = shape(feature['geometry'])
        if geometry.geom_type not in ('Polygon', 'MultiPolygon') or geometry.is_empty or not geometry.is_valid:
            raise ValueError(f'Invalid geometry: {identifier}')
        west, south, east, north = geometry.bounds
        if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
            raise ValueError(f'{identifier}: invalid lon/lat bounds')
        parts.add(props['part'])
        features[identifier] = {**feature, 'properties': props, 'geometry': geometry}
    if not features:
        raise ValueError('No polygons found')
    if len(parts) != 1:
        raise ValueError('Do not mix local and total catchments')
    return list(features.values())


def routing_metadata(identifier, props):
    """Retain explicit routing semantics; filtered files may omit the intake feature."""
    record = {key: props[key] for key in ('catchment_role', 'diversion_intake_project',
                                         'routing_requires_operations') if key in props}
    if ('routing_requires_operations' in record
            and type(record['routing_requires_operations']) is not bool):
        raise ValueError(f'{identifier}: routing_requires_operations must be a JSON boolean')
    role = record.get('catchment_role', 'dam_outlet')
    if not isinstance(role, str) or role not in {'dam_outlet', 'natural_reach_at_tailrace'}:
        raise ValueError(f'{identifier}: unknown catchment_role')
    if role == 'natural_reach_at_tailrace':
        intake = record.get('diversion_intake_project')
        if (not isinstance(intake, str) or not intake or intake.strip() != intake
                or intake == identifier or record.get('routing_requires_operations') is not True):
            raise ValueError(f'{identifier}: a tailrace reach requires another diversion_intake_project '
                             'and routing_requires_operations: true')
    elif 'diversion_intake_project' in record:
        raise ValueError(f'{identifier}: diversion_intake_project requires a tailrace reach role')
    return record


def routing_warning(identifier, record):
    return (f'{identifier}: natural drainage at the tailrace includes lateral runoff along the '
            f'bypassed river. Turbine inflow also depends on diversion from '
            f'{record["diversion_intake_project"]} and operating decisions; '
            'local meteorology alone does not represent turbine inflow.')


def forcing_metadata(features, selected=None):
    """Validate declared shared units without grouping unlabeled equal geometries."""
    references, project_forcing = {}, {}
    for feature in features:
        props, geometry = feature['properties'], feature['geometry']
        identifier = props['id']
        group = props.get('forcing_group', identifier)
        if not isinstance(group, str) or not group or group.strip() != group:
            raise ValueError(f'{identifier}: forcing_group must be a nonempty string')
        if identifier in project_forcing:
            raise ValueError(f'Duplicate project identifier: {identifier}')
        if group in references:
            other_geometry, other_part = references[group]
            if props['part'] != other_part or not geometry.equals(other_geometry):
                raise ValueError(f'Forcing group {group} must have the same geometry and part')
        references[group] = (geometry, props['part'])
        record = {'forcing_group': group, **routing_metadata(identifier, props)}
        for key in ('geometry_status', 'shared_outlet_projects'):
            if key in props:
                record[key] = props[key]
        members = record.get('shared_outlet_projects', [identifier])
        if (not isinstance(members, list) or (members and identifier not in members)
                or any(not isinstance(member, str) or not member.strip() for member in members)
                or len(set(members)) != len(members)):
            raise ValueError(f'{identifier}: invalid shared_outlet_projects')
        project_forcing[identifier] = record
    if selected is not None:
        project_forcing = {identifier: project_forcing[identifier] for identifier in selected}
    groups, warnings = {}, []
    for identifier, record in sorted(project_forcing.items()):
        groups.setdefault(record['forcing_group'], []).append(identifier)
        if record.get('catchment_role') == 'natural_reach_at_tailrace':
            warnings.append(routing_warning(identifier, record))
    for group, identifiers in sorted(groups.items()):
        shared = len(identifiers) > 1 or any(
            project_forcing[identifier].get('geometry_status') == 'shared_unit_approximation'
            or len(project_forcing[identifier].get('shared_outlet_projects', [])) > 1
            for identifier in identifiers)
        if shared:
            warnings.append(f'Forcing group {group} uses a shared catchment approximation. '
                            'Do not sum member catchment areas or derived water volumes; '
                            'count the forcing group once.')
    return dict(project_forcing=project_forcing, forcing_groups=groups,
                forcing_group_count=len(groups), warnings=warnings)
