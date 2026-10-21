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
from plot_daily import AXIS, GRID, INK, INK_SECONDARY, LABELS, SERIES, SURFACE, setup, style


PANELS = ['precipitation_mm', 'snowfall_mm', 'pet_hargreaves_mm', 'tmean_c', 'tmin_c', 'tmax_c',
          'shortwave_down_mean_wm2', 'relative_humidity_pct', 'vapor_pressure_deficit_kpa']
AMOUNTS = {'precipitation_mm', 'snowfall_mm', 'pet_hargreaves_mm'}
TEMPERATURES = ['tmean_c', 'tmin_c', 'tmax_c']
ROOT_AXES = {'precipitation_mm', 'snowfall_mm'}
DUMBBELLS = ['precipitation_mm', 'tmin_c', 'tmean_c', 'tmax_c']
NAMES = ['AORC', 'ERA5-Land', 'ERA5-Land corrected']
COLORS = dict(zip(NAMES, SERIES))
WIDTHS = {'AORC': 3, 'ERA5-Land': 1.5, 'ERA5-Land corrected': 1.5}
LEVELS = np.r_[np.arange(1, 100), 99.5, 99.9]
SHADE, BAND = '#efeee9', '#f5f4f0'
HEADER = 1.


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


def frame(plt, shape, size, title, note, names, *, dots=False, left=.7, bottom=.55, hspace=.5, wspace=.22,
          **kwargs):
    """Panels below a header band holding the title, a one-line note and the legend; margins in inches."""
    from matplotlib.lines import Line2D
    width, height = size
    fig, axes = plt.subplots(*shape, figsize=size, **kwargs)
    fig.subplots_adjust(left=left / width, right=1 - .25 / width, bottom=bottom / height,
                        top=1 - HEADER / height, hspace=hspace, wspace=wspace)
    fig.text(.25 / width, 1 - .36 / height, title, fontsize=14, fontweight='bold', color=INK, va='center')
    fig.text(.25 / width, 1 - .66 / height, note, fontsize=9.5, color=INK_SECONDARY, va='center')
    handles = [Line2D([], [], color=COLORS[name], marker='o', linestyle='', markersize=7) if dots
               else Line2D([], [], color=COLORS[name], linewidth=WIDTHS[name]) for name in names]
    fig.legend(handles, names, loc='center right', bbox_to_anchor=(1 - .2 / width, 1 - .36 / height),
               ncol=len(names), handlelength=2.4, columnspacing=1.6, fontsize=9.5)
    return fig, axes


def year_axis(ax, first, last):
    import matplotlib.dates as mdates
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y'))
    ax.xaxis.set_minor_locator(mdates.MonthLocator((4, 7, 10)))
    ax.tick_params(which='minor', length=2, color=AXIS)
    ax.set_xlim(first, last)


def series_figure(plt, sources, x, end, title, note):
    fig, axes = frame(plt, (3, 3), (13, 8.4), title, note, NAMES, hspace=.3, sharex=True)
    first, last = date(x[0].year, x[0].month, 1), x[-1] + timedelta(days=16)
    for ax, variable in zip(axes.flat, PANELS):
        ax.axvspan(end, last, color=SHADE, linewidth=0, zorder=0)
        for name in NAMES:
            ax.plot(x, sources[name][variable], color=COLORS[name], linewidth=WIDTHS[name])
        style(ax, *heading(variable))
        year_axis(ax, first, last)
    return fig


def plot_bias_by_month(plt, sources, paired, months, note):
    fig, axes = frame(plt, (3, 3), (13, 8.4), 'ERA5-Land minus AORC by calendar month', note, NAMES[1:],
                      hspace=.3, sharex=True)
    for ax, variable in zip(axes.flat, PANELS):
        a = sources['AORC'][variable]
        ax.axhline(0, color=INK_SECONDARY, linewidth=1, zorder=1)
        for name in NAMES[1:]:
            e = sources[name][variable]
            values = [difference(a[rows], e[rows], variable)
                      for rows in (paired[months[paired] == month] for month in range(1, 13))]
            ax.plot(range(1, 13), values, color=COLORS[name], linewidth=2, marker='o', markersize=5.5,
                    markeredgecolor=SURFACE, markeredgewidth=1, zorder=2)
        ax.set_xticks(range(1, 13), list('JFMAMJJASOND'))
        ax.set_xlim(.5, 12.5)
        style(ax, LABELS[variable][0], difference_units(variable))
    shared = [axes.flat[PANELS.index(variable)] for variable in TEMPERATURES]
    low = min(ax.get_ylim()[0] for ax in shared)
    high = max(ax.get_ylim()[1] for ax in shared)
    for ax in shared:
        ax.set_ylim(low, high)
    return fig


def root_axes(ax, high):
    """Square-root axes, so the many small daily amounts are not squeezed against zero."""
    from matplotlib.ticker import FixedLocator
    functions = (lambda v: np.sqrt(np.maximum(v, 0)), np.square)
    ticks = [t for t in (0, 1, 5, 10, 20, 50, 100, 200, 500) if t <= high]
    for axis, scale in ((ax.xaxis, ax.set_xscale), (ax.yaxis, ax.set_yscale)):
        scale('function', functions=functions)
        axis.set_major_locator(FixedLocator(ticks))


def plot_quantiles(plt, sources, paired, note):
    fig, axes = frame(plt, (3, 3), (12.5, 13), 'Daily values: ERA5-Land quantiles against AORC', note,
                      NAMES[1:], left=.85, bottom=.75, hspace=.42, wspace=.3)
    for ax, variable in zip(axes.flat, PANELS):
        reference = np.percentile(sources['AORC'][variable][paired], LEVELS)
        curves = {name: np.percentile(sources[name][variable][paired], LEVELS) for name in NAMES[1:]}
        low = min(reference.min(), *(c.min() for c in curves.values()))
        high = max(reference.max(), *(c.max() for c in curves.values()))
        if variable in ROOT_AXES:
            low, high = 0, high * 1.04
        else:
            low, high = low - .03 * (high - low), high + .03 * (high - low)
        ax.plot([low, high], [low, high], color=AXIS, linewidth=1.2, linestyle=(0, (4, 3)), zorder=1)
        for name, values in curves.items():
            ax.plot(reference, values, color=COLORS[name], linewidth=2, zorder=2)
        if variable in ROOT_AXES:
            root_axes(ax, high)
        ax.set_xlim(low, high)
        ax.set_ylim(low, high)
        ax.set_box_aspect(1)
        style(ax, *heading(variable))
        ax.grid(axis='x', color=GRID, linewidth=.6)
        ax.set_xlabel('AORC', fontsize=8.5)
        if variable == 'precipitation_mm':
            wet = [np.mean(sources[name][variable][paired] > WET_MM) for name in NAMES]
            lines = [f'Days over {WET_MM:g} mm'] + [f'{name}  {share:.0%}' for name, share in zip(NAMES, wet)]
            ax.text(.05, .95, '\n'.join(lines), transform=ax.transAxes, va='top', fontsize=8.5,
                    color=INK_SECONDARY, linespacing=1.5)
    for ax in axes[:, 0]:
        ax.set_ylabel('ERA5-Land', fontsize=8.5)
    return fig


def plot_catchments(plt, sources, paired, projects, note):
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
    fig, axes = frame(plt, (1, len(DUMBBELLS)), (13, 1.6 + .23 * len(names)),
                      'ERA5-Land minus AORC in each catchment', note, NAMES[1:], dots=True, left=1.65,
                      wspace=.12, sharey=True)
    for ax, variable in zip(axes, DUMBBELLS):
        for row in y[::2]:
            ax.axhspan(row - .5, row + .5, color=BAND, linewidth=0, zorder=0)
        raw = np.array([values['ERA5-Land', variable, p] for p in names])
        fixed = np.array([values['ERA5-Land corrected', variable, p] for p in names])
        ax.axvline(0, color=INK_SECONDARY, linewidth=1, zorder=1)
        ax.hlines(y, raw, fixed, color=AXIS, linewidth=1.5, zorder=2)
        for name, points in (('ERA5-Land', raw), ('ERA5-Land corrected', fixed)):
            ax.scatter(points, y, s=34, color=COLORS[name], edgecolor=SURFACE, linewidth=.8, zorder=3)
        style(ax, LABELS[variable][0], difference_units(variable))
        ax.grid(axis='y', visible=False)
        ax.grid(axis='x', color=GRID, linewidth=.6)
    axes[0].set_yticks(y, names, fontsize=8, color=INK_SECONDARY)
    axes[0].tick_params(axis='y', length=0)
    axes[0].set_ylim(len(names) - .5, -.5)
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
    names = sorted(set(projects))
    years = sorted({dates[i].year for i in paired})
    overlap = f'{years[0]}-{years[-1]}'
    fitted = 'Corrected ERA5-Land in a year with AORC is fitted without that year. Grey: no AORC.'
    plt = setup()
    from matplotlib.backends.backend_pdf import PdfPages
    overall = {name: {v: mean_by(stamp - first, source[v], count) for v in PANELS} for name, source in sources.items()}
    figures = {
        'timeseries.png': series_figure(plt, overall, x, end, f'Monthly means, {dates[0]:%Y-%m} to {dates[-1]:%Y-%m}',
                                        f'Mean of {len(names)} catchments. {fitted}'),
        'monthly_bias.png': plot_bias_by_month(
            plt, sources, paired, months, f'Days with AORC, {overlap}, all catchments. Precipitation as % of the '
            'AORC total, other variables in their own units; on the dark line there is no difference.'),
        'quantiles.png': plot_quantiles(
            plt, sources, paired, f'1st to 99.9th percentile of daily values, days with AORC, {overlap}, all '
            'catchments. On the dashed line the distributions agree; precipitation and snowfall on '
            'square-root axes.'),
        'catchments.png': plot_catchments(
            plt, sources, paired, projects, f'Days with AORC, {overlap}. Precipitation as % of the AORC total. '
            'Sorted by the uncorrected precipitation difference.'),
    }
    for name, fig in figures.items():
        fig.savefig(output_dir / name, dpi=200)
        plt.close(fig)
    index = np.searchsorted(names, projects) * count + stamp - first
    by_project = {name: {v: mean_by(index, source[v], len(names) * count).reshape(len(names), count)
                         for v in PANELS} for name, source in sources.items()}
    with PdfPages(output_dir / 'catchments.pdf') as pdf:
        for i, project in enumerate(names):
            fig = series_figure(plt, {name: {v: values[v][i] for v in PANELS} for name, values in by_project.items()},
                                x, end, f'{project}: monthly means', fitted)
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
