"""Read explicit project outlets and dataset settings."""

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
                       {"region", "level", "version", "include_virtual_connections"}, "dataset")
    region = options.get("region")
    level = options.get("level", 12)
    version = options.get("version", "1c")
    virtual = options.get("include_virtual_connections", False)
    if not isinstance(region, str) or region not in {"af", "ar", "as", "au", "eu", "gr", "na", "sa", "si"}:
        raise ValueError("dataset.region must be one of af, ar, as, au, eu, gr, na, sa, si")
    if type(level) is not int or not 1 <= level <= 12:
        raise ValueError("dataset.level must be an integer from 1 to 12")
    if version != "1c":
        raise ValueError("Only HydroBASINS standard version 1c is supported")
    if type(virtual) is not bool:
        raise ValueError("include_virtual_connections must be a YAML boolean")
    rows = document.get("projects")
    if not isinstance(rows, list) or not rows:
        raise ValueError("projects must be a nonempty list")
    projects, ids, units, codes = [], set(), set(), set()
    metadata_keys = {"river", "country", "owner", "kind", "note", "mw", "project_code"}
    for index, row in enumerate(rows):
        _mapping(row, {"id", "name", "outlet"} | metadata_keys, f"project {index + 1}")
        project_id = _text(row.get("id"), "project.id")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", project_id):
            raise ValueError(f"Invalid project ID: {project_id}")
        if project_id.casefold() in ids:
            raise ValueError(f"Duplicate project ID: {project_id}")
        outlet = _mapping(row.get("outlet"),
                          {"hybas_id", "lon", "lat", "source", "reference"}, f"{project_id}.outlet")
        unit = outlet.get("hybas_id")
        if type(unit) is not int or unit <= 0:
            raise ValueError(f"{project_id}: hybas_id must be a positive integer")
        if unit in units:
            raise ValueError(f"{project_id}: two projects select the same HydroBASINS unit {unit}; use finer delineation")
        coordinates = []
        for key, limit in (("lon", 180), ("lat", 90)):
            value = outlet.get(key)
            if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > limit:
                raise ValueError(f"{project_id}: {key} must be a finite coordinate within ±{limit}")
            coordinates.append(float(value))
        for key in ("source", "reference"):
            if key in outlet:
                _text(outlet[key], f"{project_id}.outlet.{key}")
        metadata = {key: row[key] for key in metadata_keys if key in row}
        for key, value in metadata.items():
            if key == "mw":
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise ValueError(f"{project_id}: mw must be a finite nonnegative number")
            else:
                _text(value, f"{project_id}.{key}")
        code = metadata.get("project_code", project_id)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", code) or code.casefold() in codes:
            raise ValueError(f"Invalid or duplicate project code: {code}")
        codes.add(code.casefold())
        projects.append(Project(project_id, _text(row.get("name"), f"{project_id}.name"),
                                unit, *coordinates, outlet.get("source"), outlet.get("reference"), metadata))
        ids.add(project_id.casefold())
        units.add(unit)
    return Config(Dataset(region, level, version, virtual), tuple(projects))
