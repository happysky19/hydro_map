"""Inspect catchments and write a download plan without making network requests."""

import argparse
from datetime import date
import hashlib
import json
from pathlib import Path

import yaml

from hydro_map.basins import area_km2
from hydro_map.plotting import load_features


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
    pilots = config['pilot_projects']
    unknown = set(pilots) - {p['id'] for p in projects}
    if unknown:
        raise ValueError(f'Pilot projects are absent from GeoJSON: {sorted(unknown)}')
    days = (end - start).days + 1
    return {
        'status': 'planning_only_not_source_approval',
        'input_file': geojson.name,
        'input_sha256': hashlib.sha256(geojson.read_bytes()).hexdigest(),
        'period': config['period'],
        'expected_gregorian_days_per_project': days,
        'expected_project_day_rows': len(projects) * days,
        'project_count': len(projects),
        'pilot_projects': pilots,
        'sources': config['sources'],
        'projects': projects,
        'unresolved_checks': [
            'Provider grid-cell bounds and fractional polygon coverage',
            'All variables and timestamps, including adjacent-day boundary hours',
            'AORC corrected-mask release and affected cells',
            'Daymet calendar alignment and full-polygon extraction',
            'ERA5 and ERA5-Land authenticated sample retrieval',
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
