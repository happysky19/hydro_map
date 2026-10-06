"""Inspect catchments and write a download plan without making network requests."""

import argparse
from calendar import isleap
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path

import yaml

from hydro_map.basins import area_km2
from hydro_map.plotting import load_features


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
        record = {'forcing_group': group}
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


def make_plan(geojson, config):
    geojson = Path(geojson)
    if geojson.suffix.lower() not in {'.geojson', '.json'}:
        raise ValueError('Input must be a WGS84 GeoJSON file')
    start = date.fromisoformat(config['period']['start'])
    end = date.fromisoformat(config['period']['end'])
    if start > end:
        raise ValueError('Period start must not follow end')
    if config['period']['daily_time_zone'] != 'UTC':
        raise ValueError('This research plan currently supports UTC days only')
    features = load_features(geojson)
    projects, identifiers, parts = [], set(), set()
    for feature in features:
        props, geometry = feature['properties'], feature['geometry']
        identifier = props.get('id')
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError('Each catchment needs a nonempty string identifier')
        key = identifier.strip().casefold()
        if key in identifiers:
            raise ValueError(f'Duplicate project identifier: {identifier}')
        identifiers.add(key)
        part = props.get('part')
        if part not in {'local', 'total'}:
            raise ValueError(f'{identifier}: specify properties.part as local or total')
        parts.add(part)
        projects.append({
            'id': identifier, 'name': feature['name'], 'part': part,
            'area_km2': area_km2(geometry),
            'bounds_west_south_east_north': list(geometry.bounds),
            'geometry_sha256': hashlib.sha256(geometry.normalize().wkb).hexdigest(),
        })
    if len(parts) != 1:
        raise ValueError('Do not mix local and total catchments in one extraction plan')
    forcing = forcing_metadata(features)
    for project in projects:
        project.update(forcing['project_forcing'][project['id']])
    pilots = config['pilot_projects']
    unknown = set(pilots) - {p['id'] for p in projects}
    if unknown:
        raise ValueError(f'Pilot projects are absent from GeoJSON: {sorted(unknown)}')
    days = (end - start).days + 1
    daymet_gaps = [date(year, 12, 31).isoformat()
                   for year in range(start.year, end.year + 1)
                   if isleap(year) and start <= date(year, 12, 31) <= end]
    return {
        'status': 'planning_only_not_source_approval',
        'input_file': geojson.name,
        'input_sha256': hashlib.sha256(geojson.read_bytes()).hexdigest(),
        'period': config['period'],
        'expected_gregorian_days_per_project': days,
        'expected_project_day_rows': len(projects) * days,
        'hour_ending_precipitation_window_utc': [
            f'{start.isoformat()}T01:00:00Z',
            f'{(end + timedelta(days=1)).isoformat()}T00:00:00Z'],
        'daymet_calendar_gaps': daymet_gaps,
        'expected_daymet_records_per_project': days - len(daymet_gaps),
        'project_count': len(projects),
        **forcing,
        'pilot_projects': pilots,
        'sources': config['sources'],
        'projects': projects,
        'unresolved_checks': [
            'Every required data cell and timestamp; sample audits do not establish full-period coverage',
            'AORC masking defects outside audited hours; native comparison or documented correction',
            'Daymet remaining project/time coverage and local-day versus UTC alignment',
            'ERA5-Land pre-2001 and soil-field access through the complete CDS archive',
            'ERA5 atmospheric profiles and cloud-field access',
            'Flow target, day definition, station mapping, and missing observations',
            'Out-of-sample hydrologic performance',
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--geojson', required=True, type=Path)
    parser.add_argument('--config', type=Path,
                        default=Path(__file__).with_name('data_sources.yaml'))
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    plan = make_plan(args.geojson, yaml.safe_load(args.config.read_text()))
    if args.output.resolve() in {args.geojson.resolve(), args.config.resolve()}:
        parser.error('Output must not overwrite an input file')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(plan, indent=2, allow_nan=False) + '\n')
    print(f"Planned {plan['project_count']} projects and "
          f"{plan['expected_project_day_rows']:,} project-day rows; no data downloaded.")


if __name__ == '__main__':
    main()
