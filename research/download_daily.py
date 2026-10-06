"""Download AORC, ERA5-Land and ERA5 and export aligned daily values and QC tables."""

import argparse
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


def run(geojson, start, end, output=None, *, work_dir=None, cache_dir=None,
        max_download_gb=2000, workers=4, chunk_days=7, cds_client=None):
    """Reuse the source pipelines and publish only after all stages succeed."""
    start = date.fromisoformat(start) if isinstance(start, str) else start
    end = date.fromisoformat(end) if isinstance(end, str) else end
    if start > end:
        raise ValueError('Start must not follow end')
    if not (math.isfinite(max_download_gb) and max_download_gb > 0
            and 1 <= workers <= 8 and 1 <= chunk_days <= 31):
        raise ValueError('Require a positive transfer limit, 1–8 workers and 1–31 chunk days')
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
    if cds_client is None:
        import cdsapi
        cds_client = cdsapi.Client()
    work_dir = Path(work_dir or str(output) + '.work').resolve()
    cache_dir = Path(cache_dir or work_dir / 'cache').resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    folders = [work_dir / name for name in ('aorc', 'era5-land', 'era5')]
    print(f'{len(polygons)} projects; {(end-start).days+1} UTC days; working files: {work_dir}', flush=True)
    print(f'[1/4] AORC: download, native-grid diagnostics and daily aggregation '
          f'(transfer ceiling {max_download_gb:g} GB)', flush=True)
    download_aorc.run(argparse.Namespace(
        geojson=geojson, start=start, end=end, output_dir=folders[0],
        cache_dir=cache_dir / 'aorc', projects=None, variables=download_aorc.VARIABLES,
        derive=True, workers=workers, max_download_gb=max_download_gb,
        cache_only=False, refresh_incomplete=False, keep_chunks=False))
    for index, product in enumerate(('era5-land', 'era5'), start=1):
        print(f'[{index+1}/4] {product}: download and daily catchment statistics', flush=True)
        download_cds.run_pipeline(geojson, product, folders[index], cache_dir / 'cds',
                                  start, end, chunk_days=chunk_days, client=cds_client)
    print('[4/4] Verify source manifests and export daily values and QC tables', flush=True)
    report = export_daily(geojson, folders, start, end, output)
    print(f"Saved {report['row_count']:,} project-day rows of values to {output}", flush=True)
    print(f"Quality fields: {output.with_name(report['qc_file'])}", flush=True)
    print(f'Units, quality definitions and routing notes: {output}.manifest.json', flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--geojson', required=True, type=Path)
    parser.add_argument('--start', required=True, type=date.fromisoformat)
    parser.add_argument('--end', required=True, type=date.fromisoformat)
    parser.add_argument('--output', type=Path,
                        help='Values table; also writes a sibling _qc table and manifest. '
                             'Default: outputs/catchment_daily_START_END.csv')
    parser.add_argument('--work-dir', type=Path,
                        help='Intermediate source files; default: OUTPUT.work')
    parser.add_argument('--cache-dir', type=Path,
                        help='Source cache; default: WORK_DIR/cache')
    parser.add_argument('--max-download-gb', type=float, default=2000,
                        help='AORC network-read ceiling per invocation (default: 2000 GB)')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--chunk-days', type=int, default=7,
                        help='Days per CDS request batch (1–31; default: 7)')
    args = parser.parse_args()
    try:
        run(**vars(args))
    except KeyboardInterrupt:
        print('Interrupted. Repeat the same command to resume completed periods.', file=sys.stderr)
        return 130
    except Exception as error:
        print(f'Download failed: {error}', file=sys.stderr)
        print('The requested delivery was not replaced. Completed source periods remain '
              'available for the next run.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
