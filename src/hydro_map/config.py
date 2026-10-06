"""Read explicit project outlets and dataset settings."""

from collections import defaultdict
from dataclasses import dataclass, field
import math
from pathlib import Path
import re

import yaml


@dataclass(frozen=True)
class Dataset:
    region: str
    level: int = 12
    version: str = "1c"
    include_virtual_connections: bool = False
    delineation: str = 'outlet_unit'


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    hybas_id: int
    lon: float
    lat: float
    source: str | None = None
    reference: str | None = None
    metadata: dict = field(default_factory=dict)
    grid_lon: float | None = None
    grid_lat: float | None = None
    grid_reference: str | None = None


@dataclass(frozen=True)
class Config:
    dataset: Dataset
    projects: tuple[Project, ...]


def _mapping(value, allowed, context):
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be a mapping")
    unknown = value.keys() - allowed
    if unknown:
        raise ValueError(f"Unknown {context} fields: {', '.join(sorted(map(str, unknown)))}")
    return value


def _text(value, context):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} must be a nonempty string")
    return value


def load_config(path: Path) -> Config:
    path = Path(path)
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ValueError(f"Invalid YAML: {error}") from error
    _mapping(document, {"dataset", "projects"}, "configuration")
    options = _mapping(document.get("dataset"),
                       {"region", "level", "version", "include_virtual_connections", "delineation"}, "dataset")
    region = options.get("region")
    level = options.get("level", 12)
    version = options.get("version", "1c")
    virtual = options.get("include_virtual_connections", False)
    delineation = options.get('delineation', 'outlet_unit')
    if not isinstance(delineation, str) or delineation not in {'outlet_unit', 'outlet_cell'}:
        raise ValueError('dataset.delineation must be outlet_unit or outlet_cell')
    if not isinstance(region, str) or region not in {"af", "ar", "as", "au", "eu", "gr", "na", "sa", "si"}:
        raise ValueError("dataset.region must be one of af, ar, as, au, eu, gr, na, sa, si")
    if type(level) is not int or not 1 <= level <= 12:
        raise ValueError("dataset.level must be an integer from 1 to 12")
    if version != "1c":
        raise ValueError("Only HydroBASINS standard version 1c is supported")
    if type(virtual) is not bool:
        raise ValueError("include_virtual_connections must be a YAML boolean")
    if delineation == 'outlet_cell' and virtual:
        raise ValueError('Outlet-cell delineation follows physical flow; virtual connections are unsupported')
    rows = document.get("projects")
    if not isinstance(rows, list) or not rows:
        raise ValueError("projects must be a nonempty list")
    projects, ids, codes = [], set(), set()
    units, groups = defaultdict(list), defaultdict(list)
    metadata_keys = {"river", "country", "owner", "kind", "note", "mw", "project_code", "outlet_group"}
    for index, row in enumerate(rows):
        _mapping(row, {"id", "name", "outlet"} | metadata_keys, f"project {index + 1}")
        project_id = _text(row.get("id"), "project.id")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", project_id):
            raise ValueError(f"Invalid project ID: {project_id}")
        if project_id.casefold() in ids:
            raise ValueError(f"Duplicate project ID: {project_id}")
        outlet = _mapping(row.get("outlet"),
                          {"hybas_id", "lon", "lat", "source", "reference", "grid_lon", "grid_lat", "grid_reference"}, f"{project_id}.outlet")
        unit = outlet.get("hybas_id")
        if type(unit) is not int or unit <= 0:
            raise ValueError(f"{project_id}: hybas_id must be a positive integer")
        coordinates = []
        for key, limit in (("lon", 180), ("lat", 90)):
            value = outlet.get(key)
            if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > limit:
                raise ValueError(f"{project_id}: {key} must be a finite coordinate within ±{limit}")
            coordinates.append(float(value))
        for key in ("source", "reference"):
            if key in outlet:
                _text(outlet[key], f"{project_id}.outlet.{key}")
        grid = [outlet.get('grid_lon'), outlet.get('grid_lat')]
        if delineation == 'outlet_cell' or any(value is not None for value in grid):
            for key, value, limit in zip(('grid_lon', 'grid_lat'), grid, (180, 90)):
                if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > limit:
                    raise ValueError(f'{project_id}: {key} must be a finite WGS84 coordinate')
            _text(outlet.get('grid_reference'), f'{project_id}.outlet.grid_reference')
        metadata = {key: row[key] for key in metadata_keys if key in row}
        for key, value in metadata.items():
            if key == "mw":
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise ValueError(f"{project_id}: mw must be a finite nonnegative number")
            else:
                _text(value, f"{project_id}.{key}")
        group = metadata.get('outlet_group')
        if group is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", group):
            raise ValueError(f"Invalid outlet group: {group}")
        code = metadata.get("project_code", project_id)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", code) or code.casefold() in codes:
            raise ValueError(f"Invalid or duplicate project code: {code}")
        codes.add(code.casefold())
        projects.append(Project(project_id, _text(row.get("name"), f"{project_id}.name"),
                                unit, *coordinates, outlet.get("source"), outlet.get("reference"), metadata,
                                *grid, outlet.get('grid_reference')))
        ids.add(project_id.casefold())
        units[unit].append(projects[-1])
        if group is not None:
            groups[group.casefold()].append(projects[-1])
    for unit, members in units.items():
        names = [p.metadata.get('outlet_group') for p in members]
        if delineation == 'outlet_unit' and len(members) > 1 and (None in names or len(set(names)) != 1):
            raise ValueError(f"Projects select the same HydroBASINS unit {unit}; declare one shared outlet_group or use finer delineation")
    for group, members in groups.items():
        if len({p.metadata['outlet_group'] for p in members}) != 1:
            raise ValueError(f'Outlet group {group} must use consistent spelling and case')
        if len({p.hybas_id for p in members}) != 1:
            raise ValueError(f"Outlet group {group} spans different HydroBASINS units")
        if len(members) < 2 or group in ids:
            raise ValueError(f"Outlet group {group} requires at least two projects and an ID distinct from project IDs")
    return Config(Dataset(region, level, version, virtual, delineation), tuple(projects))
