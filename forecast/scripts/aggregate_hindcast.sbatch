#!/usr/bin/env bash
# Daily catchment means of one hindcast archive, one month of starts per array task.
#
#   sbatch --export=ALL [-A ACCOUNT -p PARTITION] forecast/scripts/aggregate_hindcast.sbatch \
#       SYSTEM ARCHIVE GEOJSON OUTPUT [FIRST_MONTH]
#
# Submit from the repository root: the job runs there and puts forecast/src and history/src
# on PYTHONPATH. PYTHON (default python) and the rest of the environment come from the
# submitting shell, which --export=ALL passes on where a cluster does not by default.
# SYSTEM is aifs or wn2, ARCHIVE the folder of start folders YYYYMMDDHH, GEOJSON the catchment
# polygons (the _catchments.geojson beside the delivery) and OUTPUT the folder whose SYSTEM
# subfolder receives the tables. Task 0 is FIRST_MONTH (default 2023-01); the array runs
# 36 months. Starts already written are skipped, so a task can be resubmitted after a failure.
#SBATCH --job-name=fc-aggregate
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=03:00:00
#SBATCH --array=0-35
#SBATCH --output=fc-aggregate_%A_%a.out
set -euo pipefail

if (( $# < 4 )); then
    echo 'Usage: sbatch aggregate_hindcast.sbatch SYSTEM ARCHIVE GEOJSON OUTPUT [FIRST_MONTH]' >&2
    exit 2
fi
start=$(date -u -d "${5:-2023-01}-01 + ${SLURM_ARRAY_TASK_ID:-0} month" +%F)
end=$(date -u -d "$start + 1 month - 1 day" +%F)
export PYTHONPATH=$PWD/forecast/src:$PWD/history/src
"${PYTHON:-python}" -m catchment_forecast.aggregate --system "$1" --archive "$2" --geojson "$3" \
    --output "$4" --start "$start" --end "$end"
