"""Check a daily delivery and its QC companion; write reports and diagnostic plots."""

import argparse
from collections import Counter
import csv
from datetime import date, timedelta
import gzip
from itertools import zip_longest
import json
from pathlib import Path
import sys

import numpy as np

from export_daily import QUALITY, digest


def table_rows(path, columns):
    if path.suffix == '.parquet':
        import pyarrow.parquet as pq
        table = pq.ParquetFile(path)
        if set(table.schema.names) != set(columns):
            raise ValueError(f'{path.name}: columns differ from manifest')
        for batch in table.iter_batches(batch_size=2048):
            yield from batch.to_pylist()
    else:
        opener = gzip.open if path.suffix == '.gz' else open
        with opener(path, 'rt', newline='') as stream:
            reader = csv.DictReader(stream)
            if (len(reader.fieldnames or []) != len(columns)
                    or set(reader.fieldnames or []) != set(columns)):
                raise ValueError(f'{path.name}: columns differ from manifest')
            yield from reader


def _number(value):
    return np.nan if value is None or value == '' else float(value)


def load_delivery(path):
    manifest = json.loads(Path(str(path)+'.manifest.json').read_text())
    if manifest.get('schema_version') != 2 or manifest.get('day') != 'UTC':
        raise ValueError('Requires a schema-version 2 UTC delivery with separate QC')
    qc_name = manifest['qc_file']
    if Path(qc_name).name != qc_name or qc_name == path.name:
        raise ValueError('QC must be a separate sibling file')
    qc_path = path.with_name(qc_name)
    for table, key in ((path, 'output_sha256'), (qc_path, 'qc_output_sha256')):
        if digest(table) != manifest[key]:
            raise ValueError(f'{table.name}: hash differs from delivery manifest')
    projects = sorted(manifest['project_metadata'])
    start, end = map(date.fromisoformat, (manifest['start'], manifest['end']))
    days = [start+timedelta(days=i) for i in range((end-start).days+1)]
    columns = [name for name in manifest['columns'] if name not in ('date', 'project_id')]
    if (not days or not projects or not columns or manifest['project_count'] != len(projects)
            or manifest['row_count'] != len(days)*len(projects)):
        raise ValueError('Manifest dimensions do not define a complete project-day grid')
    # The 43-project, 30-year, 45-variable matrix occupies about 162 MiB.
    # QC strings are counted while streaming rather than retained for every row.
    values = np.full((len(days), len(projects), len(columns)), np.nan)
    counts, qc_errors = Counter(), []
    freezing = {c: dict(project_days=manifest['row_count'], valid_project_days=0,
                        valid_hours_range=None, min_valid_area_fraction_range=None, qc_counts=Counter())
                for c in columns if manifest['columns'][c].get('source') == 'era5_cds'
                and manifest['columns'][c].get('variable', '').startswith('freezing_level_')}
    failure_count = 0
    row_count = 0
    for i, (row, quality) in enumerate(zip_longest(
            table_rows(path, manifest['columns']), table_rows(qc_path, manifest['qc_columns']))):
        if row is None or quality is None or i >= manifest['row_count']:
            raise ValueError('Values and QC row counts differ from the manifest')
        d, p = divmod(i, len(projects))
        key = (str(days[d]), projects[p])
        if any((r['date'], r['project_id']) != key for r in (row, quality)):
            raise ValueError(f'Values/QC keys do not align in date/project order at row {i+1}')
        for j, column in enumerate(columns):
            value = _number(row[column])
            qc, hours, expected, area = (quality[column+'__'+field] for field in QUALITY)
            counts[(projects[p], column, str(qc))] += 1
            try:
                hours, expected, area = map(float, (hours, expected, area))
                valid = (qc == 'valid' and hours == expected == 24 and 1-1e-9 <= area <= 1+1e-9)
                coherent = (isinstance(qc, str) and bool(qc.strip()) and 0 <= hours <= expected == 24
                            and hours.is_integer() and 0 <= area <= 1+1e-9
                            and ((valid and np.isfinite(value))
                                 or (qc != 'valid' and np.isnan(value))))
            except (TypeError, ValueError):
                valid, coherent = False, False
                hours, area = np.nan, np.nan
            if not coherent:
                failure_count += 1
                if len(qc_errors) < 5:
                    qc_errors.append(dict(date=key[0], project_id=key[1], column=column))
            if valid and np.isfinite(value):
                values[d, p, j] = value
            if column in freezing:
                detail = freezing[column]
                detail['valid_project_days'] += int(valid and np.isfinite(value))
                detail['qc_counts'][str(qc)] += 1
                for field, number in (('valid_hours_range', hours),
                                      ('min_valid_area_fraction_range', area)):
                    if np.isfinite(number):
                        previous = detail[field]
                        detail[field] = ([number, number] if previous is None
                                         else [min(previous[0], number), max(previous[1], number)])
        row_count += 1
    if row_count != manifest['row_count']:
        raise ValueError('Missing project-day rows')
    check = dict(check='QC/value agreement', evaluated=values.size, failed=failure_count,
                 examples=qc_errors)
    return manifest, days, projects, columns, values, counts, check, freezing


def consistency_checks(manifest, days, projects, columns, values):
    lookup = {(info['source'], info['variable']): i for i, column in enumerate(columns)
              for info in [manifest['columns'][column]]}
    checks = []

    def check(source, label, names, calculate, relation='equal'):
        if any((source, name) not in lookup for name in names):
            return
        arrays = [values[:, :, lookup[source, name]] for name in names]
        eligible = np.logical_and.reduce([np.isfinite(a) for a in arrays])
        left, right = calculate(*arrays)
        tolerance = 5e-5+1e-6*np.abs(right)
        bad = (np.abs(left-right) > tolerance if relation == 'equal'
               else left < right-tolerance) & eligible
        examples = [dict(date=str(days[d]), project_id=projects[p])
                    for d, p in np.argwhere(bad)[:5]]
        checks.append(dict(check=f'{source}: {label}', evaluated=int(eligible.sum()),
                           failed=int(bad.sum()), examples=examples))

    for source in sorted({s for s, _ in lookup}):
        check(source, 'minimum <= mean temperature', ['tmean_c', 'tmin_c'],
              lambda mean, low: (mean, low), 'greater')
        check(source, 'mean <= maximum temperature', ['tmax_c', 'tmean_c'],
              lambda high, mean: (high, mean), 'greater')
        check(source, 'wet bulb <= air temperature', ['tmean_c', 'wet_bulb_temperature_c'],
              lambda air, wet: (air, wet), 'greater')
        check(source, 'rain + snow = precipitation', ['rainfall_mm', 'snowfall_mm', 'precipitation_mm'],
              lambda rain, snow, total: (rain+snow, total))
        check(source, 'mean speed >= magnitude of mean vector',
              ['wind_speed_ms', 'u_wind_ms', 'v_wind_ms'],
              lambda speed, u, v: (speed, np.hypot(u, v)), 'greater')
        for kind in ('shortwave', 'longwave'):
            check(source, f'{kind} energy = mean flux * 0.0864',
                  [f'{kind}_down_energy_mjm2', f'{kind}_down_mean_wm2'],
                  lambda energy, flux: (energy, flux*.0864))
        check(source, 'net energy = mean flux * 0.0864',
              ['net_radiation_energy_mjm2', 'net_radiation_mean_wm2'],
              lambda energy, flux: (energy, flux*.0864))
        check(source, 'net shortwave + net longwave = net radiation',
              ['net_shortwave_energy_mjm2', 'net_longwave_energy_mjm2', 'net_radiation_energy_mjm2'],
              lambda sw, lw, total: (sw+lw, total))
        check(source, 'root-zone thickness weighting',
              ['root_zone_soil_moisture_0_100cm_m3m3']+
              [f'soil_moisture_layer{i}_m3m3' for i in (1, 2, 3)],
              lambda root, a, b, c: (root, .07*a+.21*b+.72*c))
    for (source, name), index in lookup.items():
        units = manifest['columns'][columns[index]]['units']
        nonnegative = (name in {'precipitation_mm', 'rainfall_mm', 'snowfall_mm', 'snowmelt_mm',
                               'snow_water_equivalent_mm', 'snow_depth_m', 'snow_density_kgm3',
                               'wind_speed_ms', 'pet_hargreaves_mm', 'vapor_pressure_deficit_kpa',
                               'freezing_level_above_terrain_m', 'surface_pressure_pa',
                               'relative_humidity_pct'} or '_down_' in name)
        bounds = ((0, 1) if units in ('m3/m3', 'kg/kg') or name == 'cloud_cover_fraction'
                  else (0, 100) if name == 'snow_cover_pct'
                  else (-273.15, None) if units == 'degC'
                  else (0, None) if nonnegative else None)
        if bounds:
            check(source, f'{name} lower bound', [name], lambda a, b=bounds[0]: (a, b), 'greater')
            if bounds[1] is not None:
                check(source, f'{name} upper bound', [name], lambda a, b=bounds[1]: (b, a), 'greater')
    return checks, lookup


def comparison(x, y, project, variable):
    valid = np.isfinite(x) & np.isfinite(y)
    x, y = x[valid], y[valid]
    diff = y-x
    return dict(project_id=project, variable=variable, paired_days=len(x),
                bias_land_minus_aorc=float(diff.mean()) if len(x) else None,
                mae=float(np.abs(diff).mean()) if len(x) else None,
                rmse=float(np.sqrt(np.mean(diff**2))) if len(x) else None,
                correlation=float(np.corrcoef(x, y)[0, 1])
                if len(x) >= 3 and np.ptp(x) > 1e-12 and np.ptp(y) > 1e-12 else None)


def write_csv(path, rows, columns):
    with path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def plot_delivery(folder, manifest, days, projects, columns, values, lookup):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    colors = {'aorc_v1.1': '#176b91', 'era5_land_cds': '#ce6c32', 'era5_cds': '#6c52a3'}
    labels = {'aorc_v1.1': 'AORC', 'era5_land_cds': 'ERA5-Land', 'era5_cds': 'ERA5'}
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10,
                         'axes.spines.top': False, 'axes.spines.right': False,
                         'axes.titleweight': 'bold', 'savefig.facecolor': 'white'})
    span = f'{days[0]} to {days[-1]} UTC'
    fig, ax = plt.subplots(figsize=(max(10, len(projects)*.25+6), max(5, len(columns)*.26+2.8)),
                           layout='constrained')
    coverage = np.isfinite(values).mean(axis=0).T*100
    im = ax.imshow(coverage, aspect='auto', vmin=0, vmax=100, cmap='cividis')
    ax.set_xticks(range(len(projects)), projects, rotation=90, fontsize=8)
    names = [f"{labels.get(manifest['columns'][c]['source'], manifest['columns'][c]['source'])} | "
             + c.split('__', 1)[-1] for c in columns]
    ax.set_yticks(range(len(columns)), names, fontsize=8)
    ax.set_title('Valid daily coverage by project and variable\n'+span, pad=14)
    fig.colorbar(im, ax=ax, label='Valid days (%)', shrink=.6)
    fig.savefig(folder/'coverage.png', dpi=160)
    plt.close(fig)

    fig, axes = plt.subplots(2, 2, figsize=(10, 9), layout='constrained')
    for ax, variable, title in zip(axes.flat,
            ('precipitation_mm', 'tmean_c', 'tmin_c', 'tmax_c'),
            ('Precipitation (mm/day)', 'Mean temperature (°C)',
             'Minimum temperature (°C)', 'Maximum temperature (°C)')):
        if any((s, variable) not in lookup for s in ('aorc_v1.1', 'era5_land_cds')):
            ax.text(.5, .5, 'Both sources are required', ha='center', transform=ax.transAxes)
            ax.set_title(title)
            ax.set_xticks([])
            ax.set_yticks([])
            continue
        x, y = [values[:, :, lookup[s, variable]].ravel() for s in ('aorc_v1.1', 'era5_land_cds')]
        valid = np.isfinite(x) & np.isfinite(y)
        x, y = x[valid], y[valid]
        stats = comparison(x, y, 'ALL', variable)
        if len(x):
            # Metrics use all pairs; only scatter markers are thinned for long records.
            take = np.linspace(0, len(x)-1, min(10000, len(x)), dtype=int)
            ax.scatter(x[take], y[take], s=12, alpha=.35, color='#176b91', edgecolors='none')
            lo, hi = min(x.min(), y.min()), max(x.max(), y.max())
            pad = max((hi-lo)*.05, .05)
            ax.plot([lo-pad, hi+pad], [lo-pad, hi+pad], '--', color='#6b7280', lw=1)
            ax.set_xlim(lo-pad, hi+pad)
            ax.set_ylim(lo-pad, hi+pad)
            title += f"\nn={len(x):,}  bias={stats['bias_land_minus_aorc']:.2f}  RMSE={stats['rmse']:.2f}"
        else:
            ax.text(.5, .5, 'No paired valid days', ha='center', transform=ax.transAxes)
            ax.set_xticks([])
            ax.set_yticks([])
        ax.set_title(title)
        ax.set_xlabel('AORC')
        ax.set_ylabel('ERA5-Land')
        ax.grid(alpha=.15)
    fig.suptitle('Source comparison · '+span)
    fig.savefig(folder/'source_comparison.png', dpi=160)
    plt.close(fig)

    plot_days, plot_values = days, values
    scale = 'Daily values'
    if len(days) > 366:
        month_keys = np.array([d.strftime('%Y-%m') for d in days])
        months = np.unique(month_keys)
        plot_days = [date.fromisoformat(m+'-01') for m in months]
        monthly = []
        for month in months:
            block = values[month_keys == month]
            count = np.isfinite(block).sum(axis=0)
            monthly.append(np.divide(np.nansum(block, axis=0), count,
                           out=np.full(count.shape, np.nan), where=count > 0))
        plot_values = np.array(monthly)
        scale = 'Monthly means of valid daily values'
    panels = [(['tmean_c'], '2 m air temperature', '°C'),
              (['precipitation_mm'], 'Precipitation', 'mm/day'),
              (['shortwave_down_mean_wm2', 'longwave_down_mean_wm2', 'net_radiation_mean_wm2'],
               'Surface radiation', 'W/m²'),
              (['snow_water_equivalent_mm'], 'Snow water equivalent', 'mm'),
              (['wind_speed_ms'], '10 m wind speed', 'm/s'),
              (['freezing_level_above_terrain_m'], 'Freezing level above terrain', 'm')]
    with PdfPages(folder/'timeseries.pdf') as pdf:
        for p, project in enumerate(projects):
            fig, axes = plt.subplots(3, 2, figsize=(12, 10), layout='constrained')
            for ax, (variables, title, units) in zip(axes.flat, panels):
                plotted = False
                for variable in variables:
                    for source in colors:
                        if (source, variable) not in lookup:
                            continue
                        y = plot_values[:, p, lookup[source, variable]]
                        if not np.isfinite(y).any():
                            continue
                        label = labels[source]
                        if len(variables) > 1:
                            label += ' '+{'shortwave_down_mean_wm2': 'SW down',
                                          'longwave_down_mean_wm2': 'LW down',
                                          'net_radiation_mean_wm2': 'net total'}[variable]
                        ax.plot(plot_days, y, label=label, color=colors[source], lw=1.5,
                                linestyle='--' if variable == 'longwave_down_mean_wm2' else '-',
                                marker='o' if len(plot_days) <= 14 else None, markersize=3)
                        plotted = True
                if not plotted:
                    present = any((s, v) in lookup for s in colors for v in variables)
                    ax.text(.5, .5, 'No valid values' if present else 'Variable not in this delivery',
                            ha='center', va='center', transform=ax.transAxes)
                    ax.set_yticks([])
                else:
                    ax.legend(frameon=False, fontsize=8)
                    if variables[0] in ('precipitation_mm', 'snow_water_equivalent_mm',
                                        'wind_speed_ms', 'freezing_level_above_terrain_m'):
                        ax.set_ylim(bottom=0)
                ax.set_title(title)
                ax.set_ylabel(units)
                ax.grid(alpha=.18)
                if len(days) <= 14:
                    ax.xaxis.set_major_locator(mdates.DayLocator(interval=max(1, len(days)//5)))
                    ax.xaxis.set_major_formatter(mdates.DateFormatter('%b %d'))
                else:
                    locator = mdates.AutoDateLocator(minticks=3, maxticks=6)
                    ax.xaxis.set_major_locator(locator)
                    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
                if len(plot_days) > 1:
                    ax.set_xlim(plot_days[0], plot_days[-1])
                else:
                    ax.set_xlim(plot_days[0]-timedelta(days=1), plot_days[0]+timedelta(days=1))
            name = manifest['project_metadata'][project].get('name', project)
            fig.suptitle(f'{name} · {project}\n{scale} · {span}', fontsize=15)
            pdf.savefig(fig)
            plt.close(fig)


def check_file(path, *, plots=True):
    path = Path(path).resolve()
    manifest, days, projects, columns, values, counts, qc_check, freezing = load_delivery(path)
    checks, lookup = consistency_checks(manifest, days, projects, columns, values)
    checks.insert(0, qc_check)
    comparisons = []
    for variable in ('precipitation_mm', 'tmean_c', 'tmin_c', 'tmax_c'):
        if all((s, variable) in lookup for s in ('aorc_v1.1', 'era5_land_cds')):
            x, y = [values[:, :, lookup[s, variable]] for s in ('aorc_v1.1', 'era5_land_cds')]
            comparisons += [comparison(x[:, p], y[:, p], project, variable)
                            for p, project in enumerate(projects)]
            comparisons.append(comparison(x.ravel(), y.ravel(), 'ALL', variable))
    summaries = []
    for p, project in enumerate(projects):
        for j, column in enumerate(columns):
            y = values[:, p, j]
            y = y[np.isfinite(y)]
            summaries.append(dict(project_id=project, column=column,
                                  units=manifest['columns'][column]['units'], days=len(days),
                                  valid_days=len(y), missing_days=len(days)-len(y),
                                  valid_pct=100*len(y)/len(days),
                                  minimum=float(y.min()) if len(y) else None,
                                  mean=float(y.mean()) if len(y) else None,
                                  maximum=float(y.max()) if len(y) else None))
    folder = Path(str(path)+'.checks')
    folder.mkdir(parents=True, exist_ok=True)
    (folder/'summary.json').unlink(missing_ok=True)
    write_csv(folder/'variables.csv', summaries, list(summaries[0]))
    write_csv(folder/'qc_summary.csv', [dict(project_id=p, column=c, qc=q, days=n)
              for (p, c, q), n in sorted(counts.items())], ['project_id', 'column', 'qc', 'days'])
    write_csv(folder/'checks.csv', [{**r, 'examples': json.dumps(r['examples'])} for r in checks],
              ['check', 'evaluated', 'failed', 'examples'])
    write_csv(folder/'source_comparison.csv', comparisons, list(comparison(np.array([]), np.array([]), '', '')))
    if plots:
        plot_delivery(folder, manifest, days, projects, columns, values, lookup)
    report = dict(input_file=path.name, output_sha256=manifest['output_sha256'],
                  qc_output_sha256=manifest['qc_output_sha256'], checker_sha256=digest(Path(__file__)),
                  row_count=manifest['row_count'], project_count=len(projects), start=str(days[0]),
                  end=str(days[-1]), failed_checks=sum(r['failed'] > 0 for r in checks),
                  missing_values=int(np.isnan(values).sum()), total_values=values.size,
                  checks=checks, comparisons=comparisons, plots=plots, freezing_level=freezing,
                  scope='Delivery integrity and consistency; no independent observation validation.')
    (folder/'summary.json').write_text(json.dumps(report, indent=2, allow_nan=False)+'\n')
    print(f"Checks: {report['failed_checks']} failed; {report['missing_values']:,} missing values; {folder}",
          flush=True)
    for column, detail in freezing.items():
        label = 'above terrain' if 'above_terrain' in column else 'geopotential height'
        reasons = ', '.join(f'{flag}={count}' for flag, count in sorted(detail['qc_counts'].items()))
        print(f"ERA5 freezing level ({label}): {detail['valid_project_days']}/{detail['project_days']} "
              f"valid project-days; QC project-day counts: {reasons}", flush=True)
        print(f"  Full-area valid hours/day range: {detail['valid_hours_range']}; "
              f"daily minimum valid area fraction range: {detail['min_valid_area_fraction_range']}", flush=True)
        if not detail['valid_project_days']:
            print('  No usable freezing-level values. Check the QC reasons; do not replace with zero. '
                  'The daily rule requires a unique crossing at every contributing cell and all 24 hours.',
                  flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path, help='CSV, CSV.gz or Parquet; finds sibling QC and manifest')
    args = parser.parse_args()
    try:
        return 2 if check_file(args.input)['failed_checks'] else 0
    except (OSError, ValueError, KeyError) as error:
        print(f'Check failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
