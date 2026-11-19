#!/usr/bin/env bash
# The extended-range and seasonal table of DATE (default today, UTC): the latest run of each system, mapped onto
# the history with the mappings fitted on its hindcast, as OPS/extended/DATE.parquet (see extended.py).
#
#   sbatch --export=ALL [-A ACCOUNT -p PARTITION] forecast/scripts/daily_extended.sbatch GEOJSON HINDCAST OPS [DATE]
#
# Submit from the repository root with the Python to use as PYTHON; it also runs as a plain script. HINDCAST
# holds SYSTEM_adjusted/mapping.parquet for ec46, gefs35, cfs and seas5; a system without its mapping is left
# out. Each run is fetched once, aggregated into OPS/SYSTEM and its GRIB files deleted:
#   ec46    ECMWF's extended range of DATE - 2, from the S2S archive (key and licence of the ECMWF Data Store)
#   gefs35  GEFS of DATE - 1 to 35 days, GEFS_MEMBERS members (default 31), from AWS or GEFS_SOURCE
#   cfs     CFSv2, the four runs of each of the CFS_DAYS days before DATE (default 7)
#   seas5   SEAS5 of the latest month released before DATE at 00 UTC (the 6th at 12 UTC), from the Data Store; until the
#           month's start is in, the one before
# A run that is not in yet is left out, and the table is remade when the job runs again.
#SBATCH --job-name=fc-extended
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G
#SBATCH --time=03:00:00
#SBATCH --output=fc-extended_%j.out
set -euo pipefail

if (( $# < 3 )); then
    echo 'Usage: sbatch daily_extended.sbatch GEOJSON HINDCAST OPS [DATE]' >&2
    exit 2
fi
geojson=$1 hindcast=$2 ops=$3
export PYTHONPATH=$PWD/forecast/src:$PWD/history/src
python=${PYTHON:-python}
day=$("$python" -c 'import sys; from datetime import date, datetime, timezone
print(date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else datetime.now(timezone.utc).date())' ${4:+"$4"})
before() { "$python" -c 'import sys; from datetime import date, timedelta
print(date.fromisoformat(sys.argv[1]) - timedelta(days=int(sys.argv[2])))' "$day" "$1"; }
source=()
[[ -n ${GEFS_SOURCE:-} ]] && source=(--source "$GEFS_SOURCE")
tables=()
# Map runs of one system with its mapping and add them to the table of the day.
use() {
    local system=$1 name=$2
    shift 2
    "$python" -m catchment_forecast.adjust apply --mapping "$hindcast/${system}_adjusted/mapping.parquet" \
        --forecast "$@" --output "$ops/${system}_adjusted"
    for table in "$@"; do
        tables+=(--table "$name=$ops/${system}_adjusted/$(basename "$table")")
    done
}
has() { [[ -f $hindcast/$1_adjusted/mapping.parquet ]]; }

if has ec46; then
    start=$(before 2)
    "$python" -m catchment_forecast.s2s forecast --start "$start" --end "$start" --archive "$ops/ec46_archive" \
        --geojson "$geojson" --output "$ops" --delete || echo "ec46: the run of $start failed"
    run=$ops/ec46/${start//-/}00.parquet
    [[ -f $run ]] && use ec46 ec46 "$run"
fi

if has gefs35; then
    start=$(before 1)
    "$python" -m catchment_forecast.gefs --start "$start" --end "$start" --every-day --extended --hours 840 \
        --members "${GEFS_MEMBERS:-31}" --archive "$ops/gefs35_archive" --geojson "$geojson" --output "$ops" \
        --delete ${source[@]+"${source[@]}"} || echo "gefs35: the run of $start is not complete yet"
    run=$ops/gefs35/${start//-/}00.parquet
    [[ -f $run ]] && use gefs35 gefs "$run"
fi

if has cfs; then
    first=$(before "${CFS_DAYS:-7}") last=$(before 1)
    "$python" -m catchment_forecast.cfs --start "$first" --end "$last" --every-day --cycles 0 6 12 18 \
        --archive "$ops/cfs_archive" --geojson "$geojson" --output "$ops" --delete
    runs=()
    for run in "$ops"/cfs/*.parquet; do
        name=$(basename "$run" .parquet)
        [[ $name > ${first//-/}00 || $name == ${first//-/}00 ]] && [[ $name < ${last//-/}24 ]] && runs+=("$run")
    done
    (( ${#runs[@]} )) && use cfs cfs "${runs[@]}"
fi

if has seas5; then
    month=$("$python" -c 'import sys; from datetime import date
from catchment_forecast.seas5 import latest_month
print(f"{latest_month(date.fromisoformat(sys.argv[1])):%Y-%m}")' "$day")
    "$python" -m catchment_forecast.seas5 --first "$month" --last "$month" --archive "$ops/seas5_archive" \
        --geojson "$geojson" --output "$ops" --delete || echo "seas5: the start of $month failed"
    run=$(find "$ops/seas5" -maxdepth 1 -name '*.parquet' 2>/dev/null | sort | \
        while IFS= read -r path; do
            [[ $(basename "$path") < ${month//-/}0200.parquet ]] && echo "$path"
        done | tail -1 || true)
    [[ -n $run ]] && use seas5 seas5 "$run"
fi

if (( ${#tables[@]} == 0 )); then
    echo "No extended-range run for $day" >&2
    exit 1
fi
"$python" -m catchment_forecast.extended --date "$day" --output "$ops/extended/${day//-/}.parquet" "${tables[@]}"
