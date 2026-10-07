"""Report how far a download_daily working directory has progressed; reads files only.

    python research/progress_daily.py WORK_DIR [--cache-dir CACHE_DIR]

AORC counts completed years; ERA5-Land and ERA5 count downloaded CDS requests
and processed months. Completed years and months can already be plotted.
"""

import argparse
from datetime import date
import json
from pathlib import Path
import time

from download_cds import accumulated_requests, batch_requests, date_chunks, json_hash


def ago(seconds):
    minutes = int(seconds // 60)
    return f'{minutes} min ago' if minutes < 120 else f'{minutes // 60} h ago'


def latest_change(paths):
    times = [path.stat().st_mtime for path in paths if path.exists()]
    return ago(time.time() - max(times)) if times else 'never'


def aorc_progress(folder):
    run = json.loads((folder / 'run.json').read_text())
    start, end = date.fromisoformat(run['start']), date.fromisoformat(run['end'])
    years = list(range(start.year, end.year + 1))
    done = sorted(int(path.stem.split('_')[1]) for path in folder.glob('year_*.json'))
    span = f' ({done[0]}-{done[-1]})' if done else ''
    files = list(folder.glob('*'))
    return f'{len(done)}/{len(years)} years processed{span}; last change {latest_change(files)}'


def cds_progress(folder, cache):
    run = json.loads((folder / 'run.json').read_text())
    product, area = run['product'], run['area_north_west_south_east']
    start, end = date.fromisoformat(run['start']), date.fromisoformat(run['end'])
    batch_days = run['chunk_days'] if product == 'era5-land' else 31
    from_cds = run.get('methods', {}).get('fields_from_cds', ())
    endpoints = accumulated_requests(start, end, area, from_cds) if product == 'era5-land' else []
    hashes = {json_hash({key: spec[key] for key in ['dataset', 'request']})
              for first, last in date_chunks(start, end, batch_days)
              for spec in batch_requests(product, first, last, area, endpoints)}
    manifest_path = cache / 'requests.manifest.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    downloaded = sum(1 for key in hashes if key in manifest and (cache / f'{key}.nc').exists())
    months = len(list(date_chunks(start, end, 31)))
    processed = sorted(path.stem.split('_')[1] for path in folder.glob('month_*.json'))
    span = f' ({processed[0]} to {processed[-1]})' if processed else ''
    return (f'{downloaded}/{len(hashes)} requests downloaded, {len(processed)}/{months} months processed{span}; '
            f'last change {latest_change([manifest_path, *folder.glob("*")])}')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('work_dir', type=Path, help='The --work-dir of download_daily (default OUTPUT.work)')
    parser.add_argument('--cache-dir', type=Path, help='The --cache-dir if it was set (default WORK_DIR/cache)')
    args = parser.parse_args()
    cache = args.cache_dir or args.work_dir / 'cache'
    for label, name in [('AORC', 'aorc'), ('ERA5-Land', 'era5-land'), ('ERA5', 'era5')]:
        folder = args.work_dir / name
        if not (folder / 'run.json').exists():
            print(f'{label:10s} not started')
        elif name == 'aorc':
            print(f'{label:10s} {aorc_progress(folder)}')
        else:
            print(f'{label:10s} {cds_progress(folder, cache / "cds")}')


if __name__ == '__main__':
    main()
