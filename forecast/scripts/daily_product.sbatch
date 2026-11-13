#!/usr/bin/env bash
# The forecast product of one operational start, 00 UTC of DATE (default today, UTC), with the mappings fitted
# on the hindcast and the completion fitted on every history year.
#
#   sbatch --export=ALL [-A ACCOUNT -p PARTITION] forecast/scripts/daily_product.sbatch \
#       HISTORY GEOJSON HINDCAST AIFS_ROOT WN2_ROOT OPS [DATE]
#
# Submit from the repository root with the Python to use as PYTHON, once the history table reaches DATE - 6
# (daily_update.sh) and the runs are in: it also runs as a plain script. HINDCAST is the folder of
# product_hindcast.sbatch, with the three mappings and completion_ops.json. AIFS_ROOT and WN2_ROOT hold the
# operational runs as start folders YYYYMMDDHH, the member files in the folder or one level down. Under OPS
# the job keeps the GEFS day-1 tables of the five days before the start (fetched as needed, the GRIB files
# deleted), the start tables of each system, their adjusted tables and the product, OPS/product/DATE00.parquet.
# When HINDCAST has gefs_adjusted (GEFS as a third ensemble), the start's GEFS run is fetched the same way,
# GEFS_MEMBERS members (default 11) with the weather fields to GEFS_HOURS (default 528; 384 for a morning run,
# as GEFS reaches 16 days about 06:30 UTC and 22 days only the next day). GEFS comes from NOAA's archive on AWS,
# or from GEFS_SOURCE (an address or folder with the same layout). A system whose run is not there yet is left
# out; the product is remade when the job runs again.
#SBATCH --job-name=fc-daily
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G
#SBATCH --time=02:00:00
#SBATCH --output=fc-daily_%j.out
set -euo pipefail

if (( $# < 6 )); then
    echo 'Usage: sbatch daily_product.sbatch HISTORY GEOJSON HINDCAST AIFS_ROOT WN2_ROOT OPS [DATE]' >&2
    exit 2
fi
history=$1 geojson=$2 hindcast=$3 ops=$6
export PYTHONPATH=$PWD/forecast/src:$PWD/history/src
python=${PYTHON:-python}
# The start date, and the date N days before it, as YYYY-MM-DD.
day=$("$python" -c 'import sys; from datetime import date, datetime, timezone
print(date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else datetime.now(timezone.utc).date())' ${7:+"$7"})
before() { "$python" -c 'import sys; from datetime import date, timedelta
print(date.fromisoformat(sys.argv[1]) - timedelta(days=int(sys.argv[2])))' "$day" "$1"; }
start=${day//-/}00
source=()
[[ -n ${GEFS_SOURCE:-} ]] && source=(--source "$GEFS_SOURCE")

# The five days before the start, from the GEFS archive on AWS; days already there are skipped.
"$python" -m catchment_forecast.gefs --start "$(before 5)" --end "$(before 1)" --archive "$ops/gefs_day1_archive" \
    --geojson "$geojson" --output "$ops/gefs_day1" --every-day --members 3 --hours 24 --delete \
    ${source[@]+"${source[@]}"}
gap=()
for i in 5 4 3 2 1; do
    table=$ops/gefs_day1/gefs/$(before $i | tr -d -)00.parquet
    [[ -f $table ]] && gap+=("$table")
done
if (( ${#gap[@]} )); then
    "$python" -m catchment_forecast.adjust apply --mapping "$hindcast/gefs_day1_adjusted/mapping.parquet" \
        --forecast "${gap[@]}" --output "$ops/gefs_day1_adjusted"
fi

# The start's run of each system that is in.
forecasts=()
for system in aifs wn2; do
    root=$4
    [[ $system == wn2 ]] && root=$5
    if [[ ! -d $root/$start ]]; then
        echo "$system: no run $start in $root"
        continue
    fi
    "$python" -m catchment_forecast.aggregate --system "$system" --archive "$root" --geojson "$geojson" \
        --output "$ops" --start "$day" --end "$day"
    if [[ -f $ops/$system/$start.parquet ]]; then
        "$python" -m catchment_forecast.adjust apply --mapping "$hindcast/${system}_adjusted/mapping.parquet" \
            --forecast "$ops/$system/$start.parquet" --output "$ops/${system}_adjusted"
        forecasts+=("$ops/${system}_adjusted")
    fi
done
# The start's GEFS run, when the hindcast has GEFS as a third ensemble; left out until the run is complete.
if [[ -f $hindcast/gefs_adjusted/mapping.parquet ]]; then
    "$python" -m catchment_forecast.gefs --start "$day" --end "$day" --archive "$ops/gefs_archive" \
        --geojson "$geojson" --output "$ops" --every-day --members "${GEFS_MEMBERS:-11}" --hours "${GEFS_HOURS:-528}" \
        --weather --delete ${source[@]+"${source[@]}"} || echo "gefs: the run $start is not complete yet"
    if [[ -f $ops/gefs/$start.parquet ]]; then
        "$python" -m catchment_forecast.adjust apply --mapping "$hindcast/gefs_adjusted/mapping.parquet" \
            --forecast "$ops/gefs/$start.parquet" --output "$ops/gefs_adjusted"
        forecasts+=("$ops/gefs_adjusted")
    fi
fi
if (( ${#forecasts[@]} == 0 )); then
    echo "No run of $start is in yet" >&2
    exit 1
fi
rm -f "$ops/product/$start.parquet"
"$python" -m catchment_forecast.complete run --fit "$hindcast/completion_ops.json" --history "$history" \
    --forecast "${forecasts[@]}" --gap "$ops/gefs_day1_adjusted" --output "$ops/product" --start "$day" --end "$day"
