# Authenticated Daymet sample retrieval

This reproduces the two small native-grid precipitation requests used in the source assessment. It requires `curl`, an existing Earthdata Login entry in `~/.netrc`, and `netCDF4` for decoding. Credentials are read by curl; the commands contain no credentials. Run the shell block as a script so its temporary cookie file is cleaned up on exit.

The fixed indices were located using the returned CF projection and metre-valued coordinate arrays. They select Mica at `(y=3829, x=3342)` and Brownlee at `(y=4607, x=3295)`. Each response contains a strided 2×2 grid, including two additional corners. This is an access check, not a polygon extraction routine. Recheck coordinates if the product grid changes.

```bash
set -eu
umask 077
daymet_probe_tmp=$(mktemp -d)
trap 'rm -f "$daymet_probe_tmp/cookies"; rmdir "$daymet_probe_tmp"' EXIT
touch "$daymet_probe_tmp/cookies"
daymet_probe_output=outputs/source_validation/daymet_authenticated
mkdir -p "$daymet_probe_output"
daymet_probe_base=https://opendap.earthdata.nasa.gov/collections/C2532426483-ORNL_CLOUD/granules

for year in 1996 2025; do
    case "$year" in
        1996) time_slice='58:1:60' ;;
        2025) time_slice='362:1:364' ;;
    esac
    spatial='/x[3295:47:3342];/y[3829:778:4607]'
    temporal="/time[$time_slice];/yearday[$time_slice];/time_bnds[$time_slice][0:1:1]"
    location='/lat[3829:778:4607][3295:47:3342];/lon[3829:778:4607][3295:47:3342];/lambert_conformal_conic'
    precipitation="/prcp[$time_slice][3829:778:4607][3295:47:3342]"
    curl --fail --silent --show-error --netrc --location \
        --proto '=https' --proto-redir '=https' \
        --cookie "$daymet_probe_tmp/cookies" --cookie-jar "$daymet_probe_tmp/cookies" \
        --connect-timeout 8 --max-time 45 --max-filesize 500000 \
        --get --data-urlencode "dap4.ce=$spatial;$temporal;$location;$precipitation" \
        --output "$daymet_probe_output/sample_$year.nc" \
        "$daymet_probe_base/Daymet_Daily_V4R1.daymet_v4_daily_na_prcp_$year.nc.dap.nc4"
done
```

Decode the dates, masks, coordinates and selected values:

```python
from pathlib import Path
import netCDF4

for path in sorted(Path('outputs/source_validation/daymet_authenticated').glob('sample_*.nc')):
    with netCDF4.Dataset(path) as dataset:
        time = dataset.variables['time']
        dates = netCDF4.num2date(time[:], time.units, time.calendar)
        precipitation = dataset.variables['prcp']
        print(path.name, [str(value) for value in dates], precipitation.units)
        for name, row, column in [('MICA', 0, 1), ('BROWNLEE', 1, 0)]:
            values = precipitation[:, row, column]
            if values.count() != 3:
                raise ValueError(f'{name}: sample contains missing precipitation')
            print(name, dataset.variables['lat'][row, column],
                  dataset.variables['lon'][row, column], values.tolist())
```

The checked dates were February 28–March 1, 1996 and December 29–31, 2025. The returned noon timestamps label local 24-hour days. They do not define UTC accumulation intervals. Preserve native units, masks and geometry when extending this to an areal extraction.
