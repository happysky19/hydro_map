"""Download AORC, ERA5-Land and ERA5 and deliver aligned daily catchment tables.

OUTPUT holds the requested variables; OUTPUT_full holds every variable with its
_qc table, manifest and checks; OUTPUT_README.md documents both.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import sys

os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
os.environ.setdefault('OMP_NUM_THREADS', '1')

import download_aorc
import download_cds
from export_daily import export_daily
from check_daily import check_file
from deliver_daily import companion, write_delivery


class DeliveryCheckError(RuntimeError):
    """A delivery was published but its subsequent checks need attention."""


def run(geojson, start, end, output=None, *, work_dir=None, cache_dir=None,
        max_download_gb=2000, workers=16, chunk_days=14, cds_client=None, newest_first=True,
        cds_workers=3, keep_chunks=False, era5_source='arco', land_source='cds'):
    """Run the three sources concurrently, then export, check and deliver.

    A failed source does not stop the others; repeating the command resumes every
    source from its verified completed periods.
    """
    start = date.fromisoformat(start) if isinstance(start, str) else start
    end = date.fromisoformat(end) if isinstance(end, str) else end
    if start > end:
        raise ValueError('Start must not follow end')
    if not (math.isfinite(max_download_gb) and max_download_gb > 0
            and 1 <= workers <= 32 and 1 <= chunk_days <= 31 and 1 <= cds_workers <= 8):
        raise ValueError('Require a positive transfer limit, 1–32 workers, 1–8 CDS workers and 1–31 chunk days')
    geojson = Path(geojson).resolve()
    output = Path(output or f'outputs/catchment_daily_{start}_{end}.csv').resolve()
    if (output == geojson or output.is_dir()
            or not output.name.endswith(('.csv', '.csv.gz', '.parquet'))):
        raise ValueError('Output must be a separate .csv, .csv.gz or .parquet file')
    if output.suffix == '.parquet' and importlib.util.find_spec('pyarrow') is None:
        raise ValueError('Parquet requires research/requirements-parquet.txt')
    polygons = download_aorc.load_polygons(geojson, None)
    if any(feature['properties']['part'] != 'local'
           for feature in json.loads(geojson.read_text())['features']):
        raise ValueError('The combined workflow requires local catchments for every source')
    if shutil.which('zstd') is None:
        raise ValueError('Install the zstd command-line decoder and add it to PATH')
    # Fail on missing credentials before starting the potentially large AORC read.
    clients = {}
    for product in ('era5-land', 'era5'):
        if cds_client is not None:
            clients[product] = cds_client
        elif product == 'era5' and era5_source == 'arco':
            from arco_era5 import ArcoClient
            clients[product] = ArcoClient()
        else:
            # With the Earth Data Hub, the CDS still supplies the fields the copy lacks.
            import cdsapi
            clients[product] = cdsapi.Client(progress=False)
    if land_source == 'edh' and cds_client is None:
        import edh_era5_land
        edh_era5_land.token()
    work_dir = Path(work_dir or str(output) + '.work').resolve()
    cache_dir = Path(cache_dir or work_dir / 'cache').resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    land_client = clients['era5-land']
    if land_source == 'edh' and cds_client is None:
        from edh_era5_land import EdhClient
        land_client = EdhClient(cache_dir / 'cds' / 'edh_chunks')
    folders = [work_dir / name for name in ('aorc', 'era5-land', 'era5')]
    order = 'newest first' if newest_first else 'oldest first'
    print(f'{len(polygons)} projects; {(end-start).days+1} UTC days, {order}; working files: {work_dir}', flush=True)
    print(f'[1/3] AORC, ERA5-Land and ERA5 in parallel (AORC transfer ceiling {max_download_gb:g} GB)', flush=True)
    sources = {
        'AORC': lambda: download_aorc.run(argparse.Namespace(
            geojson=geojson, start=start, end=end, output_dir=folders[0],
            cache_dir=cache_dir / 'aorc', projects=None, variables=download_aorc.VARIABLES,
            derive=True, workers=workers, max_download_gb=max_download_gb, cache_only=False,
            refresh_incomplete=False, keep_chunks=keep_chunks, newest_first=newest_first)),
        **{label: (lambda product=product, folder=folder: download_cds.run_pipeline(
               geojson, product, folder, cache_dir / 'cds', start, end, chunk_days=chunk_days,
               client=land_client if product == 'era5-land' else clients[product],
               cds_client=clients['era5-land'] if product == 'era5-land' else None,
               workers=cds_workers, newest_first=newest_first, era5_source=era5_source, land_source=land_source))
           for label, product, folder in [('ERA5-Land', 'era5-land', folders[1]), ('ERA5', 'era5', folders[2])]},
    }
    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        futures = {label: pool.submit(task) for label, task in sources.items()}
    failures = [(label, future.exception()) for label, future in futures.items() if future.exception()]
    for label, error in failures:
        print(f'{label} stopped: {error}', file=sys.stderr, flush=True)
    if failures:
        raise failures[0][1]
    print('[2/3] Verify source manifests and export daily values and QC tables', flush=True)
    full = companion(output, '_full')
    report = export_daily(geojson, folders, start, end, full)
    print(f"Saved {report['row_count']:,} project-day rows of all variables to {full}", flush=True)
    print(f"Quality fields: {full.with_name(report['qc_file'])}", flush=True)
    print(f'Units, quality definitions and routing notes: {full}.manifest.json', flush=True)
    print('[3/3] Check, plot and write the requested table and README', flush=True)
    checks, problem = None, None
    try:
        checks = check_file(full)
        if checks['failed_checks']:
            problem = f"{checks['failed_checks']} consistency checks failed; see {full}.checks"
    except Exception as error:
        problem = str(error)
    delivery = write_delivery(full, output, companion(output, '_README', '.md'), checks)
    print(f"Requested variables: {output} ({delivery['columns']} columns); notes: {delivery['readme']}", flush=True)
    if problem:
        raise DeliveryCheckError(f'Tables were saved to {output.parent}. Delivery checks need attention: '
                                 f'{problem}. Retry without downloading: python research/check_daily.py "{full}" '
                                 f'&& python research/deliver_daily.py "{full}"')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--geojson', required=True, type=Path)
    parser.add_argument('--start', required=True, type=date.fromisoformat)
    parser.add_argument('--end', required=True, type=date.fromisoformat)
    parser.add_argument('--output', type=Path,
                        help='Requested-variable table; also writes OUTPUT_full (all variables) with '
                             '_qc, manifest and checks, and OUTPUT_README.md. '
                             'Default: outputs/catchment_daily_START_END.csv')
    parser.add_argument('--work-dir', type=Path,
                        help='Intermediate source files; default: OUTPUT.work')
    parser.add_argument('--cache-dir', type=Path,
                        help='Source cache; default: WORK_DIR/cache')
    parser.add_argument('--max-download-gb', type=float, default=2000,
                        help='AORC network-read ceiling per invocation (default: 2000 GB)')
    parser.add_argument('--workers', type=int, default=16, help='Concurrent AORC reads (1–32; default: 16)')
    parser.add_argument('--cds-workers', type=int, default=3,
                        help='Requests kept in the CDS queue per product (1–8; default: 3)')
    parser.add_argument('--era5-source', choices=['arco', 'cds'], default='arco',
                        help='ERA5 provider: the public ARCO-ERA5 copy on Google Cloud (default; no queue) or the CDS')
    parser.add_argument('--land-source', choices=['cds', 'edh'], default='cds',
                        help='ERA5-Land provider: the CDS queue (default) or the Earth Data Hub copy (needs a DestinE token)')
    parser.add_argument('--keep-chunks', action='store_true',
                        help='Keep raw AORC chunks in the cache to reprocess later without downloading')
    parser.add_argument('--oldest-first', dest='newest_first', action='store_false',
                        help='Download from the start date forward (default: latest data first)')
    parser.add_argument('--chunk-days', type=int, default=14,
                        help='Days per CDS request batch (1–31; default: 14)')
    args = parser.parse_args()
    try:
        run(**vars(args))
    except KeyboardInterrupt:
        print('Interrupted. Repeat the same command to resume completed periods.', file=sys.stderr, flush=True)
        # Worker threads may be waiting on the CDS queue; every output is written atomically,
        # so leave at once instead of waiting for them.
        sys.stdout.flush()
        os._exit(130)
    except DeliveryCheckError as error:
        print(str(error), file=sys.stderr)
        return 2
    except Exception as error:
        print(f'Download failed: {error}', file=sys.stderr)
        print('The requested delivery was not replaced. Completed source periods remain '
              'available for the next run.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
