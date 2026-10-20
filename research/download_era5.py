"""Download ERA5-Land and ERA5 daily catchment values without AORC, latest months first.

    python research/download_era5.py --geojson CATCHMENTS --start 2022-01-01 --end 2026-09-30 --output outputs/era5_2022_2026.csv

OUTPUT has the layout of download_daily.py's _full table (with _qc and
.manifest.json beside it) and only the era5_land_cds__ and era5_cds__ columns.
ERA5-Land is read from the Earth Data Hub and ERA5 from ARCO-ERA5; blanks in
either copy, and hours the Earth Data Hub does not hold yet, come from the CDS.
Repeating the command resumes from the finished months.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import os
from pathlib import Path
import sys

os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
os.environ.setdefault('OMP_NUM_THREADS', '1')

import download_cds
from export_daily import export_daily


def run(geojson, start, end, output, work_dir=None, land_source='edh', cds_client=None):
    if start > end:
        raise ValueError('Start must not follow end')
    geojson, output = Path(geojson).resolve(), Path(output).resolve()
    if output == geojson or not output.name.endswith(('.csv', '.csv.gz', '.parquet')):
        raise ValueError('Output must be a separate .csv, .csv.gz or .parquet file')
    work_dir = Path(work_dir or str(output) + '.work').resolve()
    cache = work_dir / 'cache' / 'cds'
    cache.mkdir(parents=True, exist_ok=True)
    if cds_client is None:
        import cdsapi
        cds_client = cdsapi.Client(progress=False)
    if land_source == 'edh':
        from edh_era5_land import EdhClient, token
        token()
        land = EdhClient(cache / 'edh_chunks')
    else:
        land = cds_client
    from arco_era5 import ArcoClient
    clients = {'era5-land': land, 'era5': ArcoClient()}
    print(f'ERA5-Land and ERA5, {start} to {end}, latest first; working files: {work_dir}', flush=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {product: pool.submit(
            download_cds.run_pipeline, geojson, product, work_dir / product, cache, start, end,
            client=client, cds_client=cds_client, newest_first=True, land_source=land_source)
            for product, client in clients.items()}
    errors = [(product, future.exception()) for product, future in futures.items() if future.exception()]
    for product, error in errors:
        print(f'{product} stopped: {error}', file=sys.stderr, flush=True)
    if errors:
        raise errors[0][1]
    report = export_daily(geojson, [work_dir / 'era5-land', work_dir / 'era5'], start, end, output)
    print(f"Saved {report['row_count']:,} project-day rows to {output}", flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--geojson', required=True, type=Path)
    parser.add_argument('--start', required=True, type=date.fromisoformat)
    parser.add_argument('--end', required=True, type=date.fromisoformat)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--work-dir', type=Path, help='Downloads and monthly files; default: OUTPUT.work')
    parser.add_argument('--land-source', choices=['edh', 'cds'], default='edh',
                        help='ERA5-Land from the Earth Data Hub (default; token in ~/.netrc) or the CDS queue')
    args = parser.parse_args()
    try:
        run(**vars(args))
    except KeyboardInterrupt:
        print('Interrupted. Repeat the same command to resume.', file=sys.stderr, flush=True)
        sys.stdout.flush()
        os._exit(130)
    except Exception as error:
        print(f'Download failed: {error}. Repeat the same command to resume.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
