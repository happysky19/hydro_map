"""Plot AORC beside ERA5-Land before and after quantile mapping.

    python research/plot_debias.py --aorc outputs/catchment_daily_1996_2025_full.csv --era5 outputs/era5_2022_2026.csv --output-dir figures/debias

Inputs are those of debias_era5.py. Corrected ERA5-Land in a year with AORC is
mapped with a fit on the other years, so it is compared with AORC out of sample;
later years use a fit on every AORC year, as in the delivered table.

  timeseries.png    monthly means over all catchments
  monthly_bias.png  difference from AORC by calendar month, days with AORC only
  quantiles.png     quantiles of daily values against those of AORC
  catchments.png    difference from AORC in each catchment
  catchments.pdf    the monthly means of each catchment, one page each
"""

import argparse
from datetime import date, timedelta
from pathlib import Path
import sys

import numpy as np

from debias_era5 import WET_MM, held_out, inputs, uncorrected
from plot_daily import AXIS, GRID, INK, INK_SECONDARY, LABELS, MUTED, SERIES, date_axis, setup, style


PANELS = ['precipitation_mm', 'snowfall_mm', 'pet_hargreaves_mm', 'tmean_c', 'tmin_c', 'tmax_c',
          'shortwave_down_mean_wm2', 'relative_humidity_pct', 'vapor_pressure_deficit_kpa']
AMOUNTS = {'precipitation_mm', 'snowfall_mm', 'pet_hargreaves_mm'}
DUMBBELLS = ['precipitation_mm', 'tmin_c', 'tmean_c', 'tmax_c']
NAMES = ['AORC', 'ERA5-Land', 'ERA5-Land corrected']
COLORS = dict(zip(NAMES, SERIES))
LEVELS = np.r_[np.arange(1, 100), 99.5, 99.9]


def heading(variable):
    name, units = LABELS[variable]
    return name, 'mm/day' if variable in AMOUNTS else units


def difference_units(variable):
    if variable == 'precipitation_mm':
        return '% of AORC'
    return 'percentage points' if variable.endswith('_pct') else heading(variable)[1]


def difference(a, e, variable):
    """Mean difference; for precipitation, the difference of the totals as a percentage of AORC's."""
    if not len(a):
        return np.nan
    if variable == 'precipitation_mm':
        return 100 * (e.sum() / a.sum() - 1) if a.sum() > 0 else np.nan
    return (e - a).mean()


def mean_by(index, values, size):
    finite = np.isfinite(values)
    count = np.bincount(index[finite], minlength=size)
    total = np.bincount(index[finite], values[finite], minlength=size)
    with np.errstate(invalid='ignore', divide='ignore'):
        return np.where(count > 0, total / count, np.nan)


def legend(fig, names=NAMES):
    from matplotlib.lines import Line2D
    handles = [Line2D([], [], color=COLORS[name], linewidth=2) for name in names]
    fig.legend(handles, names, loc='upper right', ncol=len(names), bbox_to_anchor=(.995, .995))


def series_figure(plt, sources, x, end, title):
    fig, axes = plt.subplots(3, 3, figsize=(13, 8.4), sharex=True)
    for ax, variable in zip(axes.flat, PANELS):
        for name in ('ERA5-Land', 'ERA5-Land corrected', 'AORC'):
            ax.plot(x, sources[name][variable], color=COLORS[name])
        ax.axvline(end, color=AXIS, linewidth=1, linestyle=(0, (4, 3)))
        style(ax, *heading(variable))
        date_axis(ax)
    axes.flat[0].annotate('AORC ends', (end, 1), xycoords=('data', 'axes fraction'), xytext=(4, -2),
                          textcoords='offset points', va='top', fontsize=8, color=MUTED)
    legend(fig)
    fig.suptitle(title, x=.01, ha='left', fontsize=12, color=INK)
    fig.tight_layout(rect=(0, 0, 1, .95))
    return fig


def plot_bias_by_month(plt, sources, paired, months):
    fig, axes = plt.subplots(3, 3, figsize=(13, 8.4), sharex=True)
    for ax, variable in zip(axes.flat, PANELS):
        a = sources['AORC'][variable]
        for name in NAMES[1:]:
            e = sources[name][variable]
            values = [difference(a[rows], e[rows], variable)
                      for rows in (paired[months[paired] == month] for month in range(1, 13))]
            ax.plot(range(1, 13), values, color=COLORS[name], marker='o', markersize=4)
        ax.axhline(0, color=AXIS, linewidth=1)
        ax.set_xticks(range(1, 13), list('JFMAMJJASOND'))
        style(ax, LABELS[variable][0], difference_units(variable))
    legend(fig, NAMES[1:])
    fig.suptitle('ERA5-Land minus AORC by calendar month, days with AORC', x=.01, ha='left', fontsize=12, color=INK)
    fig.tight_layout(rect=(0, 0, 1, .95))
    return fig


def plot_quantiles(plt, sources, paired):
    fig, axes = plt.subplots(3, 3, figsize=(12, 11))
    for ax, variable in zip(axes.flat, PANELS):
        a = sources['AORC'][variable][paired]
        reference = np.percentile(a, LEVELS)
        low, high = reference.min(), reference.max()
        for name in NAMES[1:]:
            values = np.percentile(sources[name][variable][paired], LEVELS)
            ax.plot(reference, values, color=COLORS[name], marker='o', markersize=2.5)
            low, high = min(low, values.min()), max(high, values.max())
        pad = .03 * (high - low)
        ax.plot([low - pad, high + pad], [low - pad, high + pad], color=AXIS, linewidth=1, linestyle=(0, (4, 3)))
        ax.set_xlim(low - pad, high + pad)
        ax.set_ylim(low - pad, high + pad)
        ax.set_aspect('equal')
        style(ax, *heading(variable))
        ax.set_xlabel('AORC')
        if variable == 'precipitation_mm':
            wet = [np.mean(sources[name][variable][paired] > WET_MM) for name in NAMES]
            text = '\n'.join(f'{name} {share:.0%}' for name, share in zip(NAMES, wet))
            ax.text(.04, .96, f'Days over {WET_MM:g} mm\n{text}', transform=ax.transAxes, va='top',
                    fontsize=8, color=INK_SECONDARY)
    axes.flat[0].set_ylabel('ERA5-Land')
    legend(fig, NAMES[1:])
    fig.suptitle('Quantiles of daily values (1st to 99.9th percentile), days with AORC', x=.01, ha='left',
                 fontsize=12, color=INK)
    fig.tight_layout(rect=(0, 0, 1, .96))
    return fig


def plot_catchments(plt, sources, paired, projects):
    names = sorted(set(projects[paired]))
    values = {}
    for project in names:
        rows = paired[projects[paired] == project]
        for variable in DUMBBELLS:
            for name in NAMES[1:]:
                values[name, variable, project] = difference(sources['AORC'][variable][rows],
                                                             sources[name][variable][rows], variable)
    names.sort(key=lambda project: values['ERA5-Land', DUMBBELLS[0], project])
    y = np.arange(len(names))
    fig, axes = plt.subplots(1, len(DUMBBELLS), figsize=(13, 1.4 + .21 * len(names)), sharey=True)
    for ax, variable in zip(axes, DUMBBELLS):
        raw = np.array([values['ERA5-Land', variable, p] for p in names])
        fixed = np.array([values['ERA5-Land corrected', variable, p] for p in names])
        ax.hlines(y, raw, fixed, color=GRID, linewidth=2)
        ax.scatter(raw, y, s=22, color=COLORS['ERA5-Land'], zorder=3)
        ax.scatter(fixed, y, s=22, color=COLORS['ERA5-Land corrected'], zorder=3)
        ax.axvline(0, color=AXIS, linewidth=1)
        style(ax, LABELS[variable][0], difference_units(variable))
        ax.grid(axis='x', color=GRID, linewidth=.6)
        ax.grid(axis='y', visible=False)
    axes[0].set_yticks(y, names, fontsize=7.5)
    axes[0].set_ylim(-.8, len(names) - .2)
    from matplotlib.lines import Line2D
    handles = [Line2D([], [], color=COLORS[name], marker='o', linestyle='', markersize=6) for name in NAMES[1:]]
    fig.legend(handles, NAMES[1:], loc='upper right', ncol=2, bbox_to_anchor=(.995, .999))
    fig.suptitle('ERA5-Land minus AORC in each catchment, days with AORC', x=.01, ha='left', fontsize=12, color=INK)
    fig.tight_layout(rect=(0, 0, 1, 1 - .5 / (1.4 + .21 * len(names))))
    return fig


def run(aorc_path, era5_path, output_dir):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _, _, _, aorc, land, projects, dates, latitudes = inputs(aorc_path, era5_path)
    every = np.arange(len(dates))
    sources = {'AORC': aorc, 'ERA5-Land': uncorrected(land, projects, dates, latitudes, every),
               'ERA5-Land corrected': held_out(aorc, land, projects, dates, latitudes)}
    paired = np.flatnonzero(np.logical_and.reduce(
        [np.isfinite(source[v]) for source in sources.values() for v in PANELS]))
    months = np.array([day.month for day in dates])
    stamp = np.array([day.year * 12 + day.month - 1 for day in dates])
    first, count = stamp.min(), stamp.max() - stamp.min() + 1
    x = [date((first + i) // 12, (first + i) % 12 + 1, 15) for i in range(count)]
    end = max(dates[i] for i in every if np.isfinite(aorc['precipitation_mm'][i])) + timedelta(days=1)
    span = f'{dates[0]:%Y-%m} to {dates[-1]:%Y-%m}'
    plt = setup()
    from matplotlib.backends.backend_pdf import PdfPages
    overall = {name: {v: mean_by(stamp - first, source[v], count) for v in PANELS} for name, source in sources.items()}
    figures = {
        'timeseries.png': series_figure(plt, overall, x, end, f'Monthly means over all catchments, {span}'),
        'monthly_bias.png': plot_bias_by_month(plt, sources, paired, months),
        'quantiles.png': plot_quantiles(plt, sources, paired),
        'catchments.png': plot_catchments(plt, sources, paired, projects),
    }
    for name, fig in figures.items():
        fig.savefig(output_dir / name, dpi=150)
        plt.close(fig)
    names = sorted(set(projects))
    index = np.searchsorted(names, projects) * count + stamp - first
    by_project = {name: {v: mean_by(index, source[v], len(names) * count).reshape(len(names), count)
                         for v in PANELS} for name, source in sources.items()}
    with PdfPages(output_dir / 'catchments.pdf') as pdf:
        for i, project in enumerate(names):
            fig = series_figure(plt, {name: {v: values[v][i] for v in PANELS} for name, values in by_project.items()},
                                x, end, f'{project}: monthly means, {span}')
            pdf.savefig(fig)
            plt.close(fig)
    print(f'Wrote {", ".join([*figures, "catchments.pdf"])} to {output_dir}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--aorc', required=True, type=Path, help='Full table with AORC columns (download_daily.py)')
    parser.add_argument('--era5', required=True, type=Path, help='Table written by download_era5.py')
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args()
    try:
        run(args.aorc, args.era5, args.output_dir)
    except (ValueError, OSError) as error:
        print(f'Failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
