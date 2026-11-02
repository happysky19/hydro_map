#!/usr/bin/env bash
# Bring the delivered table up to AS_OF, the last day to include; see docs/pipeline.md#daily-update.
#
#   scripts/daily_update.sh [AS_OF]
#
# AS_OF defaults to six days before today (UTC), the latest day the CDS holds: ERA5 and ERA5-Land
# reach it about five days late, and a day's ERA5-Land precipitation needs 00 UTC of the next day.
# The table is written to OUTPUT, by default DELIVERY itself; a table that already runs past AS_OF
# is cut to AS_OF, into an OUTPUT other than DELIVERY only.
set -euo pipefail

: "${DELIVERY:?the delivered table, e.g. outputs/catchment_daily_46.parquet}"
: "${AORC_FULL:?the 1996-2025 full table with AORC and ERA5-Land}"
: "${WORK:?folder for the working files and the shared download cache}"
# Every catchment of the delivery, as merge-deliveries writes them beside it.
CATCHMENTS=${CATCHMENTS:-${DELIVERY%.*}_catchments.geojson}
OUTPUT=${OUTPUT:-$DELIVERY}
PYTHON=${PYTHON:-python}
run() { "$PYTHON" -m catchment_history "$@"; }

latest=$(date -u -d '6 days ago' +%F)
as_of=$(date -u -d "${1:-$latest}" +%F)
if [[ "$as_of" > "$latest" ]]; then
    echo "The CDS holds days up to $latest; $as_of is not published yet" >&2
    exit 1
fi
last=$("$PYTHON" -c 'import sys, pyarrow.compute as pc, pyarrow.parquet as pq
print(pc.max(pq.read_table(sys.argv[1], columns=["date"])["date"]))' "$DELIVERY")
if [[ ! "$as_of" > "$last" ]]; then
    if [[ "$OUTPUT" == "$DELIVERY" ]]; then
        if [[ "$as_of" == "$last" ]]; then
            echo "Up to date: $DELIVERY ends on $last"
            exit 0
        fi
        echo "$DELIVERY ends on $last; set OUTPUT to write a copy ending on $as_of" >&2
        exit 1
    fi
    run merge "$DELIVERY" --end "$as_of" --output "$OUTPUT"
    exit 0
fi

start=$(date -u -d "$last + 1 day" +%F)
day="$WORK/${start}_$as_of"
mkdir -p "$day"
run download --geojson "$CATCHMENTS" --start "$start" --end "$as_of" --primary era5-land \
    --cache-dir "$WORK/cache" --output "$day/era5.csv"
# Catchments in the AORC table are mapped onto AORC; the others keep ERA5-Land.
run debias --aorc "$AORC_FULL" --era5 "$day/era5_full.csv" --output "$day/window.parquet"
# A day the CDS does not hold yet comes back blank; never append it.
"$PYTHON" -c 'import sys, pyarrow.parquet as pq
table = pq.read_table(sys.argv[1])
blank = {name: table[name].null_count for name in table.column_names[2:-1] if table[name].null_count}
sys.exit(f"Blank values {blank}; nothing appended, run again tomorrow" if blank else 0)' "$day/window.parquet"
run merge "$DELIVERY" "$day/window.parquet" --output "$OUTPUT"
echo "Wrote $OUTPUT: $start to $as_of appended"
