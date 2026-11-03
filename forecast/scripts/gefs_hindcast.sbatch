#!/usr/bin/env bash
# GEFS hindcast tables: one month of starts per array task, each start downloaded from NOAA's archive on AWS
# and aggregated as soon as it is down.
#
#   sbatch --export=ALL [-A ACCOUNT -p PARTITION] forecast/scripts/gefs_hindcast.sbatch \
#       ARCHIVE GEOJSON OUTPUT [FIRST_MONTH]
#
# Submit from the repository root, as aggregate_hindcast.sbatch; the compute nodes must reach AWS. ARCHIVE
# receives the GRIB files (about 10 GB a start for 31 members), OUTPUT/gefs the tables. With DELETE=1 in the
# environment a start's GRIB files are removed once its table is written. Task 0 is FIRST_MONTH (default
# 2023-01); the array runs 36 months, 12 at a time. Finished members and starts are skipped, so a task can
# be resubmitted after a failure.
#SBATCH --job-name=fc-gefs
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G
#SBATCH --time=06:00:00
#SBATCH --array=0-35%12
#SBATCH --output=fc-gefs_%A_%a.out
set -euo pipefail

if (( $# < 3 )); then
    echo 'Usage: sbatch gefs_hindcast.sbatch ARCHIVE GEOJSON OUTPUT [FIRST_MONTH]' >&2
    exit 2
fi
start=$(date -u -d "${4:-2023-01}-01 + ${SLURM_ARRAY_TASK_ID:-0} month" +%F)
end=$(date -u -d "$start + 1 month - 1 day" +%F)
export PYTHONPATH=$PWD/forecast/src:$PWD/history/src
"${PYTHON:-python}" -m catchment_forecast.gefs --start "$start" --end "$end" --archive "$1" --geojson "$2" \
    --output "$3" ${DELETE:+--delete}
