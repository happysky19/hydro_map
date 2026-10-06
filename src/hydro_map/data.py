"""Download and verify official HydroBASINS standard shapefiles."""

import hashlib
import json
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

from . import __version__


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _cached(directory: Path, names: list[str], source_url: str) -> bool:
    try:
        manifest_path = directory / "manifest.json"
        if manifest_path.is_symlink():
            return False
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return (
            manifest["source_url"] == source_url
            and set(manifest["sha256"]) == set(names)
            and all(
                not (directory / name).is_symlink()
                and _sha256(directory / name) == manifest["sha256"][name]
                for name in names
            )
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def download_dataset(region: str, level: int, version: str, cache_dir: Path) -> Path:
    """Return a verified standard HydroBASINS SHP, downloading on cache failure.

    Each dataset has its own directory containing the ZIP, four required
    shapefile components, and a manifest with their SHA256 checksums.
    """
    if region not in {"af", "ar", "as", "au", "eu", "gr", "na", "sa", "si"}:
        raise ValueError("Unsupported HydroBASINS region")
    if type(level) is not int or not 1 <= level <= 12:
        raise ValueError("HydroBASINS level must be an integer from 1 to 12")
    if version != "1c":
        raise ValueError("Only HydroBASINS version 1c is supported")

    stem = f"hybas_{region}_lev{level:02d}_v{version}"
    zip_name = f"{stem}.zip"
    components = [f"{stem}.{extension}" for extension in ("shp", "shx", "dbf", "prj")]
    names = [zip_name, *components]
    source_url = f"https://data.hydrosheds.org/file/hydrobasins/standard/{zip_name}"
    cache_dir = Path(cache_dir)
    directory = cache_dir / stem
    if directory.is_symlink():
        raise ValueError("Dataset cache directory must not be a symlink")
    if _cached(directory, names, source_url):
        return directory / components[0]

    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{stem}-", dir=cache_dir) as temporary:
        staging = Path(temporary)
        archive_path = staging / zip_name
        try:
            request = Request(source_url, headers={"User-Agent": f"hydro-map/{__version__}"})
            with urlopen(request, timeout=60) as response, archive_path.open("wb") as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
        except OSError as error:
            raise OSError(f"HydroBASINS download failed: {source_url} ({error})") from error

        try:
            with zipfile.ZipFile(archive_path) as archive:
                if archive.testzip() is not None:
                    raise ValueError("CRC mismatch")
                members = archive.infolist()
                for name in components:
                    matches = [member for member in members if member.filename == name]
                    if len(matches) != 1:
                        raise ValueError(f"Missing or duplicate component: {name}")
                    member = matches[0]
                    mode = stat.S_IFMT(member.external_attr >> 16)
                    if member.is_dir() or mode not in (0, stat.S_IFREG):
                        raise ValueError(f"Unsafe component: {name}")
                    with archive.open(member) as source, (staging / name).open("wb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
        except (zipfile.BadZipFile, ValueError, RuntimeError, EOFError) as error:
            raise ValueError(f"Invalid HydroBASINS archive: {error}") from error

        manifest = {"source_url": source_url, "sha256": {name: _sha256(staging / name) for name in names}}
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        directory.mkdir(exist_ok=True)
        for name in [*names, "manifest.json"]:
            os.replace(staging / name, directory / name)
    return directory / components[0]
