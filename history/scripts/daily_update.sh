#!/usr/bin/env bash
# Append the days published since the last run to the delivered table; see docs/pipeline.md#daily-update.
# ERA5 and ERA5-Land reach the CDS about five days late, and a day's ERA5-Land precipitation needs 00 UTC
# of the next day, so a run ends six days before today (UTC).
set -euo pipefail

: "${DELIVERY:?the delivered table, e.g. outputs/catchment_daily_46.parquet}"
: "${AORC_FULL:?the 1996-2025 full table with AORC and ERA5-Land}"
: "${COLUMBIA:?GeoJSON of the catchments in the AORC table}"
: "${PEACE:?GeoJSON of the catchments north of AORC}"
: "${WORK:?folder for the daily working files and the shared download cache}"
PYTHON=${PYTHON:-python}
run() { "$PYTHON" -m catchment_history "$@"; }

last=$("$PYTHON" -c 'import sys, pyarrow.compute as pc, pyarrow.parquet as pq
print(pc.max(pq.read_table(sys.argv[1], columns=["date"])["date"]))' "$DELIVERY")
start=$(date -u -d "$last + 1 day" +%F)
end=$(date -u -d '6 days ago' +%F)
if [[ "$start" > "$end" ]]; then
    echo "Up to date: $DELIVERY ends on $last"
    exit 0
fi
day="$WORK/$end"
mkdir -p "$day"
window=(--start "$start" --end "$end" --primary era5-land --era5-source cds --cache-dir "$WORK/cache")

run download --geojson "$COLUMBIA" "${window[@]}" --output "$day/columbia.csv"
run debias --aorc "$AORC_FULL" --era5 "$day/columbia_full.csv" --output "$day/columbia_mapped.csv"
run download --geojson "$PEACE" "${window[@]}" --output "$day/peace.csv"
run merge-deliveries "$day/columbia_mapped.csv" "$day/peace.csv" --geojson "$COLUMBIA" "$PEACE" \
    --output "$day/window.parquet"
# A day the CDS does not hold yet comes back blank; never append it.
"$PYTHON" -c 'import sys, pyarrow.parquet as pq
table = pq.read_table(sys.argv[1])
blank = {name: table[name].null_count for name in table.column_names[2:-1] if table[name].null_count}
sys.exit(f"Blank values {blank}; nothing appended, run again tomorrow" if blank else 0)' "$day/window.parquet"
run merge "$DELIVERY" "$day/window.parquet" --output "$DELIVERY"
echo "Appended $start to $end to $DELIVERY"
