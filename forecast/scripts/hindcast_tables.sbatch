#!/usr/bin/env bash
# Hindcast tables of IFS ENS (open data), CFSv2 or GEFS to 35 days: one month of starts per array task, each start
# downloaded and aggregated as soon as it is down, its GRIB files then deleted.
#
#   sbatch --export=ALL --array=0-N%10 [-A ACCOUNT -p PARTITION] forecast/scripts/hindcast_tables.sbatch \
#       SYSTEM ARCHIVE GEOJSON OUTPUT FIRST_MONTH
#
# Submit from the repository root with the Python to use as PYTHON; the compute nodes must reach the source.
# SYSTEM is ifs, cfs or gefs35; task i takes the month i months after FIRST_MONTH (YYYY-MM), on the starts of the
# AI hindcasts (days 1, 5, ..., 29), and the tables go to OUTPUT/SYSTEM. MEMBERS (default 11) is the number of
# members of ifs and gefs35; cfs takes member 01 of the 00 UTC run. A start whose table is there is skipped, so a
# task can be resubmitted after a failure.
#   ifs     from June 2024, 11 members: about 1.6 GB a start
#   cfs     from May 2023, to 183 days: about 0.4 GB a start
#   gefs35  to 35 days with net radiation and soil temperature, 11 members: about 3 GB a start
#SBATCH --job-name=fc-tables
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G
#SBATCH --time=08:00:00
#SBATCH --output=fc-tables_%A_%a.out
set -euo pipefail

if (( $# != 5 )); then
    echo 'Usage: sbatch --array=0-N hindcast_tables.sbatch SYSTEM ARCHIVE GEOJSON OUTPUT FIRST_MONTH' >&2
    exit 2
fi
system=$1 archive=$2 geojson=$3 output=$4
export PYTHONPATH=$PWD/forecast/src:$PWD/history/src
python=${PYTHON:-python}
read -r start end < <("$python" -c 'import sys; from datetime import date, timedelta
first = date.fromisoformat(sys.argv[1] + "-01")
months = first.year * 12 + first.month - 1 + int(sys.argv[2])
start = date(months // 12, months % 12 + 1, 1)
end = date(start.year + start.month // 12, start.month % 12 + 1, 1) - timedelta(days=1)
print(start, end)' "$5" "${SLURM_ARRAY_TASK_ID:-0}")
common=(--start "$start" --end "$end" --archive "$archive" --geojson "$geojson" --output "$output" --delete)
case $system in
    ifs) "$python" -m catchment_forecast.ifs "${common[@]}" --members "${MEMBERS:-11}" ;;
    cfs) "$python" -m catchment_forecast.cfs "${common[@]}" --cycles 0 ;;
    gefs35) "$python" -m catchment_forecast.gefs "${common[@]}" --members "${MEMBERS:-11}" --extended --hours 840 ;;
    *) echo "SYSTEM must be ifs, cfs or gefs35, not $system" >&2; exit 2 ;;
esac
