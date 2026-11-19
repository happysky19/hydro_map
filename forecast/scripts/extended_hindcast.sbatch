#!/usr/bin/env bash
# The extended-range and seasonal hindcasts and their mappings in one job: EC46 reforecasts from the S2S archive
# and SEAS5 reforecasts and forecasts from the Copernicus Data Store, then a mapping onto the history for each of
# ec46, gefs35, cfs and seas5 whose start tables are in OUT (gefs35 and cfs come from hindcast_tables.sbatch).
#
#   sbatch --export=ALL [-A ACCOUNT -p PARTITION] forecast/scripts/extended_hindcast.sbatch HISTORY GEOJSON OUT
#
# Submit from the repository root with the Python to use as PYTHON. The account's key is read from ~/.cdsapirc
# and the S2S and seasonal licences must have been accepted on the Data Stores, which run one request per user
# and dataset at a time; a rerun skips what is there, so the job can be resubmitted after a failure or a time
# limit. The downloads keep MEMBERS members (default 11):
#   ec46    the reforecasts of the 1st and 17th of each month of EC46_YEAR (default 2026, to October), over
#           the 20 years before it: about 400 starts, 46 days
#   seas5   the starts of each month from SEAS5_FIRST to SEAS5_LAST (default 2006-01 to 2025-12), 184 days
# Each mapping is fitted with the lead groups of adjust.EXTENDED_GROUPS, every year mapped with a fit on the
# others as for the daily systems, into OUT/SYSTEM_adjusted with check.csv and mapping.parquet; a system whose
# mapping is there is skipped. SYSTEMS (default "ec46 gefs35 cfs seas5") limits the job to some of them.
#SBATCH --job-name=fc-extended-hc
#SBATCH --cpus-per-task=2
#SBATCH --mem=48G
#SBATCH --time=48:00:00
#SBATCH --output=fc-extended-hc_%j.out
set -euo pipefail

if (( $# != 3 )); then
    echo 'Usage: sbatch extended_hindcast.sbatch HISTORY GEOJSON OUT' >&2
    exit 2
fi
history=$1 geojson=$2 out=$3
export PYTHONPATH=$PWD/forecast/src:$PWD/history/src
python=${PYTHON:-python}
systems=" ${SYSTEMS:-ec46 gefs35 cfs seas5} "
year=${EC46_YEAR:-2026}
dates=()
for month in 01 02 03 04 05 06 07 08 09 10; do
    dates+=("$year-$month-01" "$year-$month-17")
done
if [[ $systems == *' ec46 '* ]]; then
    "$python" -m catchment_forecast.s2s reforecast --dates "${dates[@]}" --years $((year - 20)) $((year - 1)) \
        --archive "$out/ec46_archive" --geojson "$geojson" --output "$out" --delete
fi
if [[ $systems == *' seas5 '* ]]; then
    "$python" -m catchment_forecast.seas5 --first "${SEAS5_FIRST:-2006-01}" --last "${SEAS5_LAST:-2025-12}" \
        --members "${MEMBERS:-11}" --archive "$out/seas5_archive" --geojson "$geojson" --output "$out" --delete
fi
groups=$("$python" -c 'from catchment_forecast.adjust import EXTENDED_GROUPS; print(*EXTENDED_GROUPS)')
for system in $systems; do
    if [[ ! -d $out/$system ]]; then
        echo "$system: no start tables in $out/$system"
        continue
    fi
    if [[ ! -f $out/${system}_adjusted/mapping.parquet ]]; then
        # shellcheck disable=SC2086
        "$python" -m catchment_forecast.adjust hindcast --forecast "$out/$system" --history "$history" \
            --geojson "$geojson" --output "$out/${system}_adjusted" --lead-groups $groups
    fi
done
