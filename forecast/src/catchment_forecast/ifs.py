"""Download IFS ENS forecasts from ECMWF's open data, only the fields the product takes from a system's weather.

    python -m catchment_forecast.ifs --start 2025-01-01 --end 2025-01-31 --archive ARCHIVE \\
        [--geojson CATCHMENTS --output OUTPUT [--delete]] [--every-day] [--members 51] [--hours 360] \\
        [--source ADDRESS]

ECMWF's open data hold the 00 UTC ENS, a control and 50 members on a 0.25
degree grid, every 3 h to 144 h and every 6 h to 360 h, one file per step with
a JSON index of its messages. The fields are those of a system's own weather in
the product: 2 m temperature and dew point and 10 m wind at every 6 h step,
and precipitation and downward shortwave, accumulated from the start, at every
24 h. Only their messages are fetched, about 145 MB a member and 7 GB a start
of 51, into ARCHIVE/YYYYMMDD00/ifs_c00.grib2 (control) and ifs_p01.grib2 to
ifs_p50.grib2; --members N takes the control and the first N - 1 members. These
fields are in the files from June 2024 (precipitation and wind earlier), with
the control until late 2026, when some days lack it; a member missing from a
start is reported and the start keeps the others.

The source is Google's copy of the open data (SOURCE), which keeps every day
from mid 2023 and serves many requests quickly; --source takes another with the
same layout, such as ECMWF's own server or a folder on disk. A start whose last
step is not there yet is reported and skipped. The starts are those of the AI
hindcasts (days 1, 5, ..., 29) or every day with --every-day. With --geojson
and --output each start is aggregated into OUTPUT/ifs as soon as it is down,
and with --delete its GRIB files are then removed.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import json
from pathlib import Path
import shutil
import sys
import time

from .gefs import fetch, starts


SOURCE = 'https://storage.googleapis.com/ecmwf-open-data'
INSTANT = ('2t', '2d', '10u', '10v')
ACCUMULATED = ('tp', 'ssrd')
HOURS = 360
STEP = 6


def url(day, step):
    return f'{SOURCE}/{day:%Y%m%d}/00z/ifs/0p25/enfo/{day:%Y%m%d}000000-{step}h-enfo-ef'


def members(count):
    """The control c00, then p01, p02, ... (count in all)."""
    return ['c00'] + [f'p{number:02d}' for number in range(1, count)]


def params(step):
    return set(INSTANT) | (set(ACCUMULATED) if step and step % 24 == 0 else set())


def wanted(index, step, names):
    """[(offset, length, member)] of the wanted messages in a step's JSON index, for the members in names."""
    found = []
    for line in index.splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry['param'] not in params(step) or entry.get('type') not in ('cf', 'pf'):
            continue
        member = 'c00' if entry['type'] == 'cf' else f'p{int(entry["number"]):02d}'
        if member in names:
            found.append((int(entry['_offset']), int(entry['_length']), member))
    return found


def check(found, step, names):
    """The members every wanted field of the step is there for; a member with only some of them stops."""
    count = {}
    for _, _, member in found:
        count[member] = count.get(member, 0) + 1
    partial = sorted(member for member, n in count.items() if n != len(params(step)))
    if partial:
        raise ValueError(f'step {step}: {", ".join(partial)} lack fields')
    return [member for member in names if member in count]


def runs_of(found):
    """Adjacent messages in one request: [(first byte, [(length, member), ...])]."""
    merged = []
    for offset, length, member in sorted(found):
        if merged and merged[-1][0] + sum(n for n, _ in merged[-1][1]) == offset:
            merged[-1][1].append((length, member))
        else:
            merged.append((offset, [(length, member)]))
    return merged


def step_messages(address, found, workers):
    """{member: bytes of its messages} of one step."""
    def get(run):
        first, parts = run
        data = fetch(address, first, first + sum(n for n, _ in parts) - 1)
        out, at = [], 0
        for length, member in parts:
            message = data[at:at + length]
            if message[:4] != b'GRIB' or len(message) != length:
                raise ValueError(f'{address}: bytes {first + at}-{first + at + length - 1} are not a GRIB message')
            out.append((member, message))
            at += length
        return out
    by_member = {}
    with ThreadPoolExecutor(workers) as pool:
        for messages in pool.map(get, runs_of(found)):
            for member, message in messages:
                by_member.setdefault(member, []).append(message)
    return {member: b''.join(parts) for member, parts in by_member.items()}


def download_start(day, folder, names, hours=HOURS, workers=16):
    """ARCHIVE/YYYYMMDD00/ifs_MEMBER.grib2 for the members in names; returns (members, bytes written)."""
    present = None
    streams, size = {}, 0
    try:
        for step in range(0, hours + 1, STEP):
            address = url(day, step)
            found = wanted(fetch(address + '.index').decode(), step, names)
            address += '.grib2'
            have = check(found, step, names)
            if present is None:
                if not have:
                    raise ValueError(f'step {step}: no requested members')
                present = have
                streams = {member: (folder / f'ifs_{member}.part').open('wb') for member in present}
            elif have != present:
                raise ValueError(f'step {step}: members {sorted(set(present) ^ set(have))} come and go')
            for member, data in step_messages(address, found, workers).items():
                streams[member].write(data)
                size += len(data)
    finally:
        for stream in streams.values():
            stream.close()
    for member in present:
        (folder / f'ifs_{member}.part').replace(folder / f'ifs_{member}.grib2')
    expected = {f'ifs_{member}.grib2' for member in present}
    for path in folder.glob('ifs_*.grib2'):
        if path.name not in expected:
            path.unlink()
    receipt = dict(requested=names, hours=hours, members=present)
    (folder / 'download.part').write_text(json.dumps(receipt))
    (folder / 'download.part').replace(folder / 'download.json')
    return present, size


def run(days, archive, names, hours=HOURS, workers=16, catchments=None, output=None, delete=False):
    from .aggregate import run_start
    for day in days:
        folder = archive / f'{day:%Y%m%d}00'
        table = None if output is None else output / f'{folder.name}.parquet'
        if table is not None and table.exists():
            continue
        try:
            fetch(url(day, hours) + '.index')
        except FileNotFoundError:
            print(f'{folder.name}: step {hours} is not there (yet), skipped', flush=True)
            continue
        receipt = folder / 'download.json'
        saved = json.loads(receipt.read_text()) if receipt.exists() else {}
        complete = (saved.get('requested') == names and saved.get('hours') == hours
                    and bool(saved.get('members'))
                    and all((folder / f'ifs_{member}.grib2').is_file() for member in saved['members']))
        if not complete:
            folder.mkdir(parents=True, exist_ok=True)
            receipt.unlink(missing_ok=True)
            begin = time.time()
            try:
                present, size = download_start(day, folder, names, hours, workers)
            except (FileNotFoundError, ValueError) as error:
                print(f'{folder.name}: skipped, {error}', flush=True)
                continue
            missing = [member for member in names if member not in present]
            print(f'{folder.name}: {len(present)} members, {size / 1e6:.0f} MB in {time.time() - begin:.0f} s'
                  + (f'; not in the files: {", ".join(missing)}' if missing else ''), flush=True)
        if output is not None:
            run_start('ifs', folder, catchments, output)
            if delete and table.exists():
                shutil.rmtree(folder)


def main(argv=None):
    global SOURCE
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--start', required=True, type=date.fromisoformat, help='First start date')
    parser.add_argument('--end', required=True, type=date.fromisoformat, help='Last start date')
    parser.add_argument('--archive', required=True, type=Path, help='Folder of start folders YYYYMMDDHH')
    parser.add_argument('--geojson', type=Path, help='Catchment polygons: aggregate each start when downloaded')
    parser.add_argument('--output', type=Path, help='Folder whose ifs subfolder receives the tables')
    parser.add_argument('--delete', action='store_true', help='Remove a start\'s GRIB files once its table exists')
    parser.add_argument('--every-day', action='store_true', help='Every day, not only the AI hindcast days')
    parser.add_argument('--members', type=int, default=51, help='Control and perturbed members (default 51)')
    parser.add_argument('--hours', type=int, default=HOURS, help=f'Last step, whole days (default {HOURS})')
    parser.add_argument('--workers', type=int, default=16, help='Parallel requests (default 16)')
    parser.add_argument('--source', default=SOURCE, help=f'Address or folder holding YYYYMMDD/ (default {SOURCE})')
    args = parser.parse_args(argv)
    if (args.geojson is None) != (args.output is None) or (args.delete and args.output is None):
        parser.error('--geojson and --output go together, and --delete needs them')
    if args.hours < 24 or args.hours % 24 or args.hours > HOURS:
        parser.error(f'--hours must be a multiple of 24 up to {HOURS}')
    catchments = output = None
    if args.output is not None:
        from .aggregate import Catchments
        catchments, output = Catchments(args.geojson), args.output / 'ifs'
        output.mkdir(parents=True, exist_ok=True)
    SOURCE = args.source.rstrip('/')
    days = starts(args.start, args.end, args.every_day)
    print(f'{len(days)} IFS ENS starts, {args.members} members to {args.hours} h from {SOURCE} into {args.archive}',
          flush=True)
    run(days, args.archive, members(args.members), args.hours, args.workers, catchments, output, args.delete)
    return 0


if __name__ == '__main__':
    sys.exit(main())
