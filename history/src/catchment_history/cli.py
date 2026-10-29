"""catchment-history COMMAND [ARGUMENTS]; COMMAND --help describes each command."""

import importlib
import os
import sys

COMMANDS = {
    'download': ('download', 'AORC, ERA5-Land and ERA5 to the full table, its checks and the requested table'),
    'download-era5': ('download_era5', 'ERA5-Land and ERA5 only, in the full-table layout'),
    'check': ('check', 'consistency, plausibility and source-agreement checks of a full table'),
    'deliver': ('deliver', 'the requested table and README from a full table'),
    'debias': ('debias', 'ERA5-Land mapped onto AORC: gaps filled, REPLACED catchments replaced'),
    'merge': ('merge', 'join requested tables of consecutive periods'),
    'merge-deliveries': ('merge_deliveries', 'join requested tables of different catchments'),
    'plot': ('plot', 'daily series from a working directory or a table'),
    'plot-debias': ('plot_debias', 'AORC beside ERA5-Land before and after the mapping'),
    'progress': ('progress', 'how far a download has progressed'),
}


def usage():
    width = max(map(len, COMMANDS))
    lines = [__doc__, '', 'Commands:'] + [f'  {name:<{width}}  {text}' for name, (_, text) in COMMANDS.items()]
    return '\n'.join(lines)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ('-h', '--help'):
        print(usage())
        return 0 if argv else 2
    if argv[0] not in COMMANDS:
        print(f'Unknown command: {argv[0]}\n\n{usage()}', file=sys.stderr)
        return 2
    os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
    os.environ.setdefault('OMP_NUM_THREADS', '1')
    module = importlib.import_module(f'catchment_history.{COMMANDS[argv[0]][0]}')
    return module.main(argv[1:])
