#!/usr/bin/env bash
# The daily run: the history table to six days before today, the forecast product of today's 00 UTC runs, then the
# extended-range and seasonal table of today (daily_extended.sbatch), when the hindcast has its mappings.
#
#   sbatch [-A ACCOUNT -p PARTITION] forecast/scripts/daily_ops.sh CONFIG
#
# Also runs from scrontab or as a plain script. CONFIG is a shell file of settings kept outside the repository:
#   REPO              the repository; the job runs from it
#   PYTHON            the Python of the forecast package (eccodes), with LD_LIBRARY_PATH if it needs one
#   HISTORY_PYTHON    the Python of the history package (cdsapi)
#   HISTORY           the delivered history table, updated in place, with its _catchments.geojson beside it
#   AORC_FULL, WORK   as history/scripts/daily_update.sh takes them
#   HINDCAST          the folder of product_hindcast.sbatch, with the mappings and completion_ops.json
#   AIFS_ROOT         the operational AIFS runs, start folders YYYYMMDDHH
#   WN2_ROOT          the same for WN2; a folder that does not exist leaves WN2 out
#   OPS               the operational folder; the product is OPS/product/YYYYMMDD00.parquet
#   GEFS_MEMBERS, GEFS_HOURS, GEFS_SOURCE, IFS_MEMBERS, IFS_SOURCE, CFS_DAYS   optional, see daily_product.sbatch
#                     and daily_extended.sbatch
# Run it once the runs are in (GEFS to day 16 about 06:30 UTC, AIFS about 06:50 UTC). A later run finds the
# history up to date and remakes the product with what has arrived since; a run while another is going stops.
# The runs of the day are aggregated even when the history update fails, since the AIFS runs leave their archive
# within days; the product then waits for the history. Each run also tries again the product of any of the
# CATCH_UP days before today (default 3) that an earlier run took up and left without one. The job ends with
# status 1 when the history update failed or today's product is missing, after everything else, so that
# --mail-type=FAIL reports it.
#SBATCH --job-name=daily-ops
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=04:00:00
#SBATCH --output=daily-ops_%j.out
set -euo pipefail

if (( $# != 1 )); then
    echo 'Usage: daily_ops.sh CONFIG' >&2
    exit 2
fi
set -a
# shellcheck disable=SC1090
source "$1"
set +a
for name in REPO PYTHON HISTORY_PYTHON HISTORY AORC_FULL WORK HINDCAST AIFS_ROOT WN2_ROOT OPS; do
    [[ -n ${!name:-} ]] || { echo "$1 does not set $name" >&2; exit 2; }
done
cd "$REPO"
mkdir -p "$OPS"
exec 9>"$OPS/.daily_ops.lock"
if ! flock -n 9; then
    echo 'Another daily run is going; stopping'
    exit 0
fi
today=$(date -u +%F)
stamp() { echo "$(date -u '+%F %T') UTC: $*"; }

problems=()
stamp "history to $(date -u -d '6 days ago' +%F)"
DELIVERY=$HISTORY PYTHON=$HISTORY_PYTHON PYTHONPATH=$REPO/history/src history/scripts/daily_update.sh \
    || problems+=('the history update failed')

product_of() {
    bash forecast/scripts/daily_product.sbatch "$HISTORY" "${HISTORY%.*}_catchments.geojson" "$HINDCAST" \
        "$AIFS_ROOT" "$WN2_ROOT" "$OPS" "$1" || echo "product of $1: not made"
}
# A day this job took up but left without a product (OPS/attempted/DATE) is tried again for CATCH_UP days.
mkdir -p "$OPS/attempted"
for back in $(seq "${CATCH_UP:-3}" -1 1); do
    past=$(date -u -d "$back days ago" +%F)
    if [[ -f $OPS/attempted/$past && ! -f $OPS/product/${past//-/}00.parquet ]]; then
        stamp "product of ${past}, missing so far"
        product_of "$past"
    fi
done
stamp "product of ${today}"
touch "$OPS/attempted/$today"
product_of "$today"

product=$OPS/product/${today//-/}00.parquet
[[ -f $product ]] || problems+=("no product of $today")
[[ -f $product ]] && "$PYTHON" -c 'import sys
import numpy as np, pyarrow.parquet as pq
table = pq.read_table(sys.argv[1])
keys = {"init_time", "system", "member", "lead_day", "valid_date", "project_id"}
column = lambda name: table[name].to_numpy(zero_copy_only=False)
system, member, lead = column("system").astype(str), column("member").astype(str), column("lead_day")
blank = sum(np.isnan(column(name).astype(float)) for name in table.column_names if name not in keys)
for name in sorted(set(system)):
    rows = system == name
    print(f"{name}: {len(set(member[rows]))} members, lead days {lead[rows].min()} to {lead[rows].max()}, "
          f"{rows.sum():,} rows, {int(blank[rows].sum())} blank values")' "$product"
for system in ec46 gefs35 cfs seas5; do
    if [[ -f $HINDCAST/${system}_adjusted/mapping.parquet ]]; then
        stamp "extended-range table of ${today}"
        bash forecast/scripts/daily_extended.sbatch "${HISTORY%.*}_catchments.geojson" "$HINDCAST" "$OPS" "$today" \
            || echo "extended-range table: not made"
        break
    fi
done
if (( ${#problems[@]} )); then
    message=$(printf '%s; ' "${problems[@]}")
    stamp "done, but: ${message%; }"
    exit 1
fi
stamp "done: $product"
