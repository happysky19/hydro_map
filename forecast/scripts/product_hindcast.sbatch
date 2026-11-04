#!/usr/bin/env bash
# The forecast product of the whole hindcast in one job: each system's start tables are mapped onto the
# history (skipped where mapping.parquet exists), the completion is fitted on the history before the
# hindcast years, and every start is completed into the columns of the history table.
#
#   sbatch --export=ALL [-A ACCOUNT -p PARTITION] forecast/scripts/product_hindcast.sbatch HISTORY GEOJSON OUT
#
# Submit from the repository root with the Python to use as PYTHON. OUT holds the start tables of aggregate
# (OUT/aifs, OUT/wn2) and of gefs with --hours 24 for every day (OUT/gefs_day1/gefs). The job writes
# OUT/aifs_adjusted, OUT/wn2_adjusted, OUT/gefs_day1_adjusted, OUT/completion.json and OUT/product with its
# check.csv. FIRST_YEAR (default 2023) is the first hindcast year; the completion is fitted before it.
#SBATCH --job-name=fc-product
#SBATCH --cpus-per-task=1
#SBATCH --mem=16G
#SBATCH --time=06:00:00
#SBATCH --output=fc-product_%j.out
set -euo pipefail

if (( $# != 3 )); then
    echo 'Usage: sbatch product_hindcast.sbatch HISTORY GEOJSON OUT' >&2
    exit 2
fi
history=$1 geojson=$2 out=$3
export PYTHONPATH=$PWD/forecast/src:$PWD/history/src
python=${PYTHON:-python}
for system in aifs wn2 gefs_day1; do
    tables=$out/$system
    [[ $system == gefs_day1 ]] && tables=$out/gefs_day1/gefs
    if [[ ! -f $out/${system}_adjusted/mapping.parquet ]]; then
        "$python" -m catchment_forecast.adjust hindcast --forecast "$tables" --history "$history" \
            --geojson "$geojson" --output "$out/${system}_adjusted"
    fi
done
"$python" -m catchment_forecast.complete fit --history "$history" --before "${FIRST_YEAR:-2023}" \
    --output "$out/completion.json"
"$python" -m catchment_forecast.complete run --fit "$out/completion.json" --history "$history" \
    --forecast "$out/aifs_adjusted" "$out/wn2_adjusted" --gap "$out/gefs_day1_adjusted" --output "$out/product"
