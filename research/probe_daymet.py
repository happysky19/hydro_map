#!/usr/bin/env python3
"""Probe two Daymet point-years and three catalog years; downloads are capped at 1 MB each."""
import argparse
import concurrent.futures
import csv
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import subprocess
import time
from urllib.parse import urlencode

VARIABLES = {"dayl", "prcp", "srad", "swe", "tmax", "tmin", "vp"}


def requests():
    for name, lat, lon, year in [("mica_1996", 52.08, -118.57, 1996), ("brownlee_2025", 44.84, -116.90, 2025)]:
        query = urlencode(dict(lat=lat, lon=lon, years=year, format="csv"))
        yield name, "https://daymet.ornl.gov/single-pixel/api/data?" + query, "csv", year
    for year in [1996, 2025, 2026]:
        query = urlencode(dict(collection_concept_id="C2532426483-ORNL_CLOUD", page_size=7,
            temporal=f"{year}-01-01T00:00:00Z,{year}-12-31T23:59:59Z", bounding_box="-119,51,-118,53"))
        yield f"catalog_{year}", "https://cmr.earthdata.nasa.gov/search/granules.json?" + query, "json", year


def fetch(item, directory):
    name, url, suffix, year = item
    output = directory / f"{name}.{suffix}"
    headers = directory / f"{name}.headers.txt"
    output.unlink(missing_ok=True)
    headers.unlink(missing_ok=True)
    start = time.monotonic()
    result = subprocess.run(["curl", "--silent", "--show-error", "--ipv4", "--location", "--connect-timeout", "8",
        "--max-time", "55", "--max-filesize", "1000000", "--dump-header", str(headers),
        "--output", str(output), "--write-out", "%{http_code}", url], capture_output=True, text=True)
    record = dict(name=name, url=url, checked_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
        exit_code=result.returncode, http_status=result.stdout, seconds=round(time.monotonic()-start, 2),
        error=result.stderr.strip())
    if not output.exists():
        return record
    body = output.read_bytes()
    record.update(bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
    if result.returncode != 0 or result.stdout != "200":
        return record
    try:
        if suffix == "json":
            entries = json.loads(body).get("feed", {}).get("entry", [])
            record["granules"] = [{k: e.get(k) for k in ["title", "time_start", "time_end", "updated"]} for e in entries]
            variables = {e["title"].split("_")[-2] for e in entries
                if e["title"].startswith("Daymet_Daily_V4R1.daymet_v4_daily_na_")
                and e["title"].endswith(f"_{year}.nc")}
            record["variables"] = sorted(variables)
            record["all_seven_na_variables_present"] = variables == VARIABLES
        else:
            lines = body.decode().splitlines()
            header = next((i for i, line in enumerate(lines) if line.startswith("year,yday,")), None)
            if header is None:
                raise ValueError("Expected Daymet CSV header was not found")
            rows = list(csv.DictReader(io.StringIO("\n".join(lines[header:]))))
            record.update(rows=len(rows), first=rows[0] if rows else None, last=rows[-1] if rows else None,
                leap_day_rows=[r for r in rows if r.get("year") == "1996" and r.get("yday") == "60"])
    except (ValueError, KeyError, UnicodeDecodeError) as error:
        record["validation_error"] = str(error)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Directory for responses, headers, and manifest")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "manifest.json").unlink(missing_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        records = list(executor.map(lambda item: fetch(item, args.output), requests()))
    (args.output / "manifest.json").write_text(json.dumps(records, indent=2) + "\n")
    for record in records:
        print(record["name"], "HTTP", record["http_status"], "curl", record["exit_code"],
            "bytes", record.get("bytes", 0), "rows", record.get("rows"),
            "variables", record.get("variables"))


if __name__ == "__main__":
    main()
