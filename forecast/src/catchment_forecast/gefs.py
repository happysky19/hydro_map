"""Download GEFS forecasts from NOAA's archive on AWS, only the fields the catchment tables use.

    python -m catchment_forecast.gefs --start 2024-01-01 --end 2024-01-31 --archive ARCHIVE \\
        [--geojson CATCHMENTS --output OUTPUT [--delete]] [--every-day] [--members 31]

GEFS v12 runs a control and 30 perturbed members; the 00 UTC run goes to 35
days on a 0.5 degree grid. The archive holds every run since 2017, and each
new run a few hours after its start. For each start and member the GRIB index
files are read and only the fields in FIELDS are fetched, by byte ranges, at
every 6 h step to 528 h (22 days), into ARCHIVE/YYYYMMDDHH/gefs_c00.grib2 to
gefs_p30.grib2, about 0.4 GB a member. The starts are those of the AI
hindcasts (00 UTC on days 1, 5, ..., 29), or every day with --every-day.

With --geojson and --output each start is aggregated into OUTPUT/gefs as soon
as it is downloaded (see aggregate), and with --delete its GRIB files are then
removed. A member file or start table already there is skipped, so a rerun
picks up where a run stopped. A start missing from the archive is reported
and skipped.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
import shutil
import sys
import time
import urllib.error
import urllib.request

from .tigge import start_dates


BUCKET = 'https://noaa-gefs-pds.s3.amazonaws.com'
# Fields by file (pgrb2a and pgrb2b), as VARIABLE:level in the GRIB index.
FIELDS = {
    'a': ('TMP:2 m above ground', 'UGRD:10 m above ground', 'VGRD:10 m above ground', 'APCP:surface',
          'DSWRF:surface', 'USWRF:surface', 'DLWRF:surface', 'ULWRF:surface', 'TCDC:entire atmosphere',
          'LHTFL:surface', 'WEASD:surface', 'SNOD:surface', 'SOILW:0-0.1 m below ground',
          'TSOIL:0-0.1 m below ground'),
    'b': ('DPT:2 m above ground', 'SOILW:0.1-0.4 m below ground', 'SOILW:0.4-1 m below ground',
          'SOILW:1-2 m below ground', 'TSOIL:0.1-0.4 m below ground', 'TSOIL:0.4-1 m below ground',
          'TSOIL:1-2 m below ground', 'HGT:0C isotherm', 'SNOHF:surface'),
}
# Fields that do not change, fetched at one step only.
ONCE = {'b': ('HGT:surface',)}
ONCE_STEP = 6
# Fields with a period (6 h means and amounts) are missing at step 0.
PERIOD = {'APCP:surface', 'DSWRF:surface', 'USWRF:surface', 'DLWRF:surface', 'ULWRF:surface',
          'TCDC:entire atmosphere', 'LHTFL:surface', 'SNOHF:surface'}
HOURS = 528
STEP = 6
# Wanted messages closer than this are fetched in one request, with the messages between them.
GAP = 100_000


def members(count):
    """GEFS member names: the control c00, then p01, p02, ... (count in all)."""
    return ['c00'] + [f'p{number:02d}' for number in range(1, count)]


def starts(first, last, every_day=False):
    if every_day:
        return [first + timedelta(days=n) for n in range((last - first).days + 1)]
    return [day for year in range(first.year, last.year + 1) for day in start_dates(year) if first <= day <= last]


def url(day, member, kind, step):
    prefix = 'gec' if member == 'c00' else 'gep'
    return (f'{BUCKET}/gefs.{day:%Y%m%d}/00/atmos/pgrb2{kind}p5/{prefix}{member[1:]}.t00z.pgrb2{kind}.0p50'
            f'.f{step:03d}')


def fetch(address, first=None, last=None):
    """Bytes of address, or of the byte range first-last (last None: to the end). A 404 raises FileNotFoundError."""
    request = urllib.request.Request(address)
    if first is not None:
        request.add_header('Range', f'bytes={first}-{"" if last is None else last}')
    for attempt in range(6):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise FileNotFoundError(address) from None
            failure = error
        except (urllib.error.URLError, OSError) as error:
            failure = error
        time.sleep(2 ** attempt)
    raise RuntimeError(f'{address}: {failure}')


def ranges(index, wanted):
    """(byte ranges (first, last or None) to fetch, wanted fields found) from a GRIB index (wgrib2 inventory)."""
    lines = [line.split(':') for line in index.splitlines() if line.strip()]
    offsets = [int(line[1]) for line in lines] + [None]
    picked = [(offsets[i], None if offsets[i + 1] is None else offsets[i + 1] - 1, f'{line[3]}:{line[4]}')
              for i, line in enumerate(lines) if f'{line[3]}:{line[4]}' in wanted]
    merged = []
    for first, last, _ in picked:
        if merged and merged[-1][1] is not None and first - merged[-1][1] - 1 < GAP:
            merged[-1] = (merged[-1][0], last)
        else:
            merged.append((first, last))
    return merged, {name for _, _, name in picked}


def step_bytes(day, member, step):
    """The wanted messages of one member and step, from both files."""
    parts = []
    for kind, wanted in FIELDS.items():
        wanted = set(wanted) | (set(ONCE.get(kind, ())) if step == ONCE_STEP else set())
        expected = wanted if step else wanted - PERIOD
        address = url(day, member, kind, step)
        found_ranges, found = ranges(fetch(address + '.idx').decode(), wanted)
        if expected - found:
            raise ValueError(f'{address}: no {", ".join(sorted(expected - found))}')
        for first, last in found_ranges:
            data = fetch(address, first, last)
            if data[:4] != b'GRIB' or (last is not None and len(data) != last - first + 1):
                raise ValueError(f'{address}: bytes {first}-{last} are not whole GRIB messages')
            parts.append(data)
    return b''.join(parts)


def download_member(day, member, folder, workers=16, hours=HOURS):
    """ARCHIVE/YYYYMMDD00/gefs_MEMBER.grib2 with every step; returns its size in bytes (0 if it was there)."""
    path = folder / f'gefs_{member}.grib2'
    if path.exists():
        return 0
    with ThreadPoolExecutor(workers) as pool:
        data = list(pool.map(lambda step: step_bytes(day, member, step), range(0, hours + 1, STEP)))
    partial = path.with_suffix('.part')
    with partial.open('wb') as stream:
        for part in data:
            stream.write(part)
    partial.replace(path)
    return path.stat().st_size


def run(days, archive, names, workers=16, hours=HOURS, catchments=None, output=None, delete=False):
    from .aggregate import run_start
    for day in days:
        folder = archive / f'{day:%Y%m%d}00'
        table = None if output is None else output / f'{folder.name}.parquet'
        if table is not None and table.exists():
            continue
        try:
            fetch(url(day, names[0], 'a', 0) + '.idx')
        except FileNotFoundError:
            print(f'{folder.name}: not in the archive, skipped', flush=True)
            continue
        folder.mkdir(parents=True, exist_ok=True)
        try:
            for member in names:
                begin = time.time()
                size = download_member(day, member, folder, workers, hours)
                if size:
                    print(f'{folder.name} {member}: {size / 1e6:.0f} MB in {time.time() - begin:.0f} s', flush=True)
        except FileNotFoundError as error:
            print(f'{folder.name}: {error} is missing, skipped', flush=True)
            continue
        if output is not None:
            run_start('gefs', folder, catchments, output)
            if delete and table.exists():
                shutil.rmtree(folder)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--start', required=True, type=date.fromisoformat, help='First start date')
    parser.add_argument('--end', required=True, type=date.fromisoformat, help='Last start date')
    parser.add_argument('--archive', required=True, type=Path, help='Folder of start folders YYYYMMDDHH')
    parser.add_argument('--geojson', type=Path, help='Catchment polygons: aggregate each start when downloaded')
    parser.add_argument('--output', type=Path, help='Folder whose gefs subfolder receives the tables')
    parser.add_argument('--delete', action='store_true', help='Remove a start\'s GRIB files once its table exists')
    parser.add_argument('--every-day', action='store_true', help='Every day, not only the AI hindcast days')
    parser.add_argument('--members', type=int, default=31, help='Control and perturbed members (default 31)')
    parser.add_argument('--workers', type=int, default=16, help='Parallel requests (default 16)')
    args = parser.parse_args(argv)
    if (args.geojson is None) != (args.output is None) or (args.delete and args.output is None):
        parser.error('--geojson and --output go together, and --delete needs them')
    catchments = output = None
    if args.output is not None:
        from .aggregate import Catchments
        catchments, output = Catchments(args.geojson), args.output / 'gefs'
        output.mkdir(parents=True, exist_ok=True)
    days = starts(args.start, args.end, args.every_day)
    print(f'{len(days)} GEFS starts, {args.members} members, into {args.archive}', flush=True)
    run(days, args.archive, members(args.members), args.workers, HOURS, catchments, output, args.delete)
    return 0


if __name__ == '__main__':
    sys.exit(main())
