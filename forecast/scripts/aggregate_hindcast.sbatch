#!/usr/bin/env bash
# Daily catchment means of one hindcast archive, one month of starts per array task.
#
#   SYSTEM=aifs ARCHIVE=... GEOJSON=... OUTPUT=... sbatch forecast/scripts/aggregate_hindcast.sbatch
#
# Task 0 is FIRST_MONTH (default 2023-01); the array runs 36 months. Starts already
# written are skipped, so a task can be resubmitted after a failure.
#SBATCH --job-name=fc-aggregate
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=03:00:00
#SBATCH --array=0-35
#SBATCH --output=fc-aggregate_%A_%a.out
set -euo pipefail

: "${SYSTEM:?aifs or wn2}"
: "${ARCHIVE:?folder of start folders YYYYMMDDHH}"
: "${GEOJSON:?catchment polygons, e.g. the _catchments.geojson beside the delivery}"
: "${OUTPUT:?output folder; tables go to OUTPUT/SYSTEM}"
REPO=${REPO:?the repository root}
PYTHON=${PYTHON:-python}

start=$(date -u -d "${FIRST_MONTH:-2023-01}-01 + ${SLURM_ARRAY_TASK_ID:-0} month" +%F)
end=$(date -u -d "$start + 1 month - 1 day" +%F)
export PYTHONPATH=$REPO/forecast/src:$REPO/history/src
"$PYTHON" -m catchment_forecast.aggregate --system "$SYSTEM" --archive "$ARCHIVE" --geojson "$GEOJSON" \
    --output "$OUTPUT" --start "$start" --end "$end"
