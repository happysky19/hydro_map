#!/usr/bin/env python3
"""Small, read-only AORC v1.1 research probe; not a production coverage audit.

Requires requests, numpy, shapely, and the zstd CLI. Samples the first six
January days at Mica, northern Mica, and Brownlee in 1996 and 2025.
Use --cache-only to prohibit network access. Downloaded bytes are capped at
50 MB per invocation. Every attempted object is recorded before access.
"""
import argparse
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import traceback

import numpy as np
import requests
from shapely.geometry import Point, box

from hydro_map.plotting import load_features

BASE_URL = "https://noaa-nws-aorc-v1-1-1km.s3.amazonaws.com/"
DOWNLOAD_LIMIT = 50_000_000
YEARS = (1996, 2025)
VARIABLES = {"TMP_2maboveground": "K", "APCP_surface": "kg/m^2"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


class Probe:
    def __init__(self, args):
        self.output = args.output
        self.cache = args.cache_dir or args.output
        self.cache_only = args.cache_only
        self.downloaded = 0
        self.zstd = shutil.which("zstd")
        self.manifest = self.output / "requests.jsonl"
        self.arrays = {}

    def record(self, event):
        event["recorded_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        with self.manifest.open("a") as stream:
            stream.write(json.dumps(event, allow_nan=False) + "\n")

    def fetch(self, key):
        url = BASE_URL + key
        cached = self.cache / key.replace("/", "__")
        destination = self.output / key.replace("/", "__")
        event = {"url": url, "event": "attempt", "cache_path": str(cached)}
        self.record(event)
        try:
            if cached.exists():
                content = cached.read_bytes()
                event.update(source="cache", path=str(cached))
            else:
                require(not self.cache_only, f"Missing cached object: {key}")
                with requests.get(url, timeout=60, stream=True) as response:
                    event.update(status=response.status_code,
                                 etag=response.headers.get("ETag"),
                                 last_modified=response.headers.get("Last-Modified"))
                    response.raise_for_status()
                    expected = int(response.headers.get("Content-Length", 0))
                    require(expected <= DOWNLOAD_LIMIT - self.downloaded,
                            "50 MB download cap would be exceeded")
                    parts = []
                    for part in response.iter_content(65536):
                        self.downloaded += len(part)
                        require(self.downloaded <= DOWNLOAD_LIMIT,
                                "50 MB download cap exceeded while streaming")
                        parts.append(part)
                    content = b"".join(parts)
                destination.write_bytes(content)
                event.update(source="network", path=str(destination))
            event.update(event="success", bytes=len(content),
                         sha256=hashlib.sha256(content).hexdigest())
            self.record(event)
            return content
        except Exception as error:
            event.update(event="failure", error=str(error),
                         downloaded_bytes=self.downloaded)
            self.record(event)
            raise

    def metadata(self, year):
        document = json.loads(self.fetch(f"{year}.zarr/.zmetadata"))
        require(document.get("zarr_consolidated_format") == 1,
                "Only consolidated Zarr v2 metadata format 1 is supported")
        metadata = document["metadata"]
        require(metadata.get(".zgroup", {}).get("zarr_format") == 2,
                "Only Zarr v2 groups are supported")
        for name in ("time", "latitude", "longitude", *VARIABLES):
            array = metadata[f"{name}/.zarray"]
            attrs = metadata[f"{name}/.zattrs"]
            dimensions = [name] if name not in VARIABLES else ["time", "latitude", "longitude"]
            require(attrs.get("_ARRAY_DIMENSIONS") == dimensions,
                    f"Unexpected dimensions for {name}")
            require(array.get("zarr_format") == 2 and array.get("order") == "C",
                    f"Unsupported format/order for {name}")
            require(array.get("dimension_separator", ".") == "." and
                    array.get("filters") is None, f"Unsupported filters/separator: {name}")
            require(array.get("compressor", {}).get("id") == "zstd",
                    f"Unsupported compressor for {name}; expected zstd")
            require(array["dtype"] == ("<i2" if name in VARIABLES else "<f8"),
                    f"Unexpected dtype for {name}")
            for field in ("shape", "chunks"):
                require(len(array[field]) == len(dimensions) and
                        all(isinstance(n, int) and n > 0 for n in array[field]),
                        f"Invalid {field} for {name}")
            require(math.prod(array["chunks"]) * np.dtype(array["dtype"]).itemsize <= 20_000_000,
                    f"Decoded {name} chunk exceeds supported 20 MB size")
        axes = [metadata[f"{axis}/.zarray"]["shape"][0]
                for axis in ("time", "latitude", "longitude")]
        for name, units in VARIABLES.items():
            attrs = metadata[f"{name}/.zattrs"]
            require(metadata[f"{name}/.zarray"]["shape"] == axes,
                    f"Coordinate/data shape mismatch for {name}")
            require(attrs.get("units") == units and attrs.get("crs") == "EPSG:4326",
                    f"Unexpected units/CRS for {name}")
            require(attrs.get("aorc_version") == "v1.1", "Unexpected AORC version")
            require(attrs.get("missing_value") == -32767 and
                    attrs.get("_FillValue", -32767) == -32767,
                    f"Unexpected missing-value encoding for {name}")
            require(metadata[f"{name}/.zarray"].get("fill_value") in (None, -32767),
                    f"Conflicting Zarr fill value for {name}")
            require(math.isfinite(attrs.get("scale_factor", float("nan"))) and
                    attrs["scale_factor"] > 0 and
                    math.isfinite(attrs.get("add_offset", 0)), "Invalid packing scale/offset")
        time_attrs = metadata["time/.zattrs"]
        require(time_attrs.get("units") == "seconds since 1970-01-01" and
                time_attrs.get("calendar") == "proleptic_gregorian",
                "Unsupported time units/calendar; expected Unix seconds and proleptic Gregorian")
        require(metadata["latitude/.zattrs"].get("units") == "degrees_north" and
                metadata["longitude/.zattrs"].get("units") == "degrees_east",
                "Unexpected coordinate units")
        return metadata

    def chunk(self, year, name, key, metadata):
        cache_key = (year, name, key)
        if cache_key not in self.arrays:
            definition = metadata[f"{name}/.zarray"]
            payload = self.fetch(f"{year}.zarr/{name}/{key}")
            raw = subprocess.run([self.zstd, "-d", "-c"], input=payload,
                                 capture_output=True, check=True).stdout
            dtype = np.dtype(definition["dtype"])
            require(len(raw) == math.prod(definition["chunks"]) * dtype.itemsize,
                    f"Unexpected decoded chunk length for {year}/{name}/{key}")
            self.arrays[cache_key] = np.frombuffer(raw, dtype=dtype).reshape(definition["chunks"])
        return self.arrays[cache_key]

    def coordinate(self, year, axis, index, metadata):
        definition = metadata[f"{axis}/.zarray"]
        require(0 <= index < definition["shape"][0], "Coordinate index out of range")
        width = definition["chunks"][0]
        return float(self.chunk(year, axis, str(index // width), metadata)[index % width])

    def nearest_coordinate(self, year, axis, target, metadata):
        """Support a regular ascending grid; verify actual chunks used in each year.

        This intentionally checks only accessed chunks, not the complete axis.
        The endpoint-derived index locates a chunk; the returned center is read
        from that year's stored coordinates and checked against the regular grid.
        """
        definition = metadata[f"{axis}/.zarray"]
        length, width = definition["shape"][0], definition["chunks"][0]
        first = self.coordinate(year, axis, 0, metadata)
        last = self.coordinate(year, axis, length - 1, metadata)
        require(length > 1 and math.isfinite(first) and math.isfinite(last) and last > first,
                f"Unsupported nonascending {axis} axis")
        step = (last - first) / (length - 1)
        require(first <= target <= last, f"Point outside {axis} coordinate envelope")
        index = int(round((target - first) / step))
        block = index // width
        values = self.chunk(year, axis, str(block), metadata)
        count = min(width, length - block * width)
        expected = first + np.arange(block * width, block * width + count) * step
        require(np.allclose(values[:count], expected, rtol=0, atol=1e-8),
                f"Accessed {axis} chunk is not on the supported regular grid")
        return index, float(values[index % width])


def iso(seconds):
    return dt.datetime.fromtimestamp(float(seconds), dt.timezone.utc).isoformat()


def run(args, probe):
    require(probe.zstd is not None, "The zstd CLI is required and was not found on PATH")
    loaded = load_features(args.geojson)
    features = {f["properties"]["id"]: f["geometry"] for f in loaded}
    require(len(features) == len(loaded), "GeoJSON project IDs must be unique")
    require({"MICA", "BROWNLEE"} <= features.keys(), "GeoJSON needs MICA and BROWNLEE IDs")
    mica = features["MICA"]
    points = {"MICA_north": mica.intersection(box(-180, mica.bounds[3] - 0.05, 180, 90)).representative_point(),
              "MICA_representative": mica.representative_point(),
              "BROWNLEE_representative": features["BROWNLEE"].representative_point()}
    results = {"input_sha256": hashlib.sha256(args.geojson.read_bytes()).hexdigest(),
        "years": {}, "points": [], "limitations": [
        "Six January days and three points only; no 30-year or full-basin coverage certification.",
        "Only accessed coordinate chunks are checked against a regular grid.",
        "April 2026 NOAA masking notice is unresolved by this probe.",
        "Daily precipitation uses end-of-hour timestamps; hourly T extrema are not native daily extremes."]}
    for year in YEARS:
        metadata = probe.metadata(year)
        time_shape = metadata["time/.zarray"]["shape"][0]
        first_time = probe.coordinate(year, "time", 0, metadata)
        last_time = probe.coordinate(year, "time", time_shape - 1, metadata)
        results["years"][str(year)] = {"time_count": time_shape,
            "first_timestamp": iso(first_time), "last_timestamp": iso(last_time),
            "coordinate_bounds": {axis: [probe.coordinate(year, axis, 0, metadata),
                probe.coordinate(year, axis, metadata[f"{axis}/.zarray"]["shape"][0] - 1, metadata)]
                for axis in ("latitude", "longitude")}}
        times = [probe.coordinate(year, "time", i, metadata) for i in range(144)]
        require(first_time == dt.datetime(year, 1, 1, tzinfo=dt.timezone.utc).timestamp() and
                np.all(np.diff(times) == 3600), "Probe requires hourly January 1-6 timestamps")
        for label, point in points.items():
            yi, latitude = probe.nearest_coordinate(year, "latitude", point.y, metadata)
            xi, longitude = probe.nearest_coordinate(year, "longitude", point.x, metadata)
            require(features[label.split("_")[0]].covers(Point(longitude, latitude)),
                    f"Nearest grid center is outside {label} catchment")
            entry = {"year": year, "label": label, "cell_center": [longitude, latitude],
                     "index_yx": [yi, xi], "variables": {}}
            for name, units in VARIABLES.items():
                definition = metadata[f"{name}/.zarray"]
                attrs = metadata[f"{name}/.zattrs"]
                nt, ny, nx = definition["chunks"]
                require(nt >= 144, "Probe requires at least 144 hours in the first data chunk")
                key = f"0.{yi // ny}.{xi // nx}"
                chunk = probe.chunk(year, name, key, metadata)
                raw = chunk[:144, yi % ny, xi % nx]
                values = np.where(raw == attrs["missing_value"], np.nan,
                                  raw * attrs["scale_factor"] + attrs.get("add_offset", 0))
                valid = np.isfinite(values)
                require(valid.all(), f"Missing sample values for {year}/{label}/{name}")
                sample = {"url": BASE_URL + f"{year}.zarr/{name}/{key}", "units": units,
                          "valid_hours": int(valid.sum()), "sample_hours": 144,
                          "first_24_values": [float(v) if np.isfinite(v) else None for v in values[:24]]}
                daily = values[1:25] if name == "APCP_surface" else values[:24]
                sample["utc_jan01_total_mm" if name == "APCP_surface" else "utc_jan01_mean_C"] = (
                    float(daily.sum()) if name == "APCP_surface" else float(daily.mean() - 273.15)
                ) if np.isfinite(daily).all() else None
                entry["variables"][name] = sample
            results["points"].append(entry)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--geojson", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--cache-only", action="store_true", help="Forbid all network requests")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    probe = Probe(args)
    status = {"status": "running", "download_limit_bytes": DOWNLOAD_LIMIT}
    (args.output / "results.json").unlink(missing_ok=True)
    (args.output / "status.json").write_text(json.dumps(status, indent=2))
    probe.record({"event": "run_started", "cache_only": args.cache_only})
    try:
        results = run(args, probe)
        (args.output / "results.json").write_text(json.dumps(results, indent=2, allow_nan=False))
        status["status"] = "success"
        print(f"Validated {len(results['points'])} point-period samples; downloaded {probe.downloaded} bytes.")
        return 0
    except Exception as error:
        status.update(status="failed", error=str(error), traceback=traceback.format_exc())
        print(f"Probe failed: {error}; evidence retained in {args.output}", file=sys.stderr)
        return 1
    finally:
        status["downloaded_bytes"] = probe.downloaded
        (args.output / "status.json").write_text(json.dumps(status, indent=2))
        probe.record({"event": "run_finished", **status})


if __name__ == "__main__":
    raise SystemExit(main())
