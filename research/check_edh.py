"""Check access to the DestinE Earth Data Hub ERA5-Land store and print its layout.

    python research/check_edh.py

Reads the personal access token from ~/.netrc
(machine data.earthdatahub.destine.eu password <token>); the token is never
printed. Shows whether the store is reachable, which variables it holds, their
encoding and attributes, and hourly values near midnight at one point, which
tell whether accumulated fields are stored as totals since 00 UTC (as in the
CDS) or as hourly amounts.
"""

import json
import netrc
from pathlib import Path

import requests


HOST = 'data.earthdatahub.destine.eu'
STORE = f'https://{HOST}/era5/era5-land-v0.zarr'
NAMES = ['t2m', 'd2m', 'sp', 'u10', 'v10', 'sd', 'sde', 'rsn', 'snowc', 'swvl1', 'stl1',
         'tp', 'sf', 'smlt', 'e', 'ssrd', 'strd', 'ssr', 'str', 'valid_time', 'latitude', 'longitude']


def main():
    try:
        token = netrc.netrc(Path.home() / '.netrc').authenticators(HOST)[2]
    except (FileNotFoundError, TypeError):
        raise SystemExit(f'Add "machine {HOST} password <token>" to ~/.netrc first')
    session = requests.Session()
    session.auth = ('edh', token)
    root = session.get(f'{STORE}/zarr.json', timeout=60)
    print('store status', root.status_code)
    if not root.ok:
        raise SystemExit('The store is not reachable with this token')
    arrays = root.json().get('consolidated_metadata', {}).get('metadata', {})
    print('arrays:', sorted(arrays) if arrays else 'not consolidated')
    for name in NAMES:
        meta = arrays.get(name)
        if meta is None:
            response = session.get(f'{STORE}/{name}/zarr.json', timeout=60)
            meta = response.json() if response.ok else {'missing': response.status_code}
        summary = {key: meta.get(key) for key in ('missing', 'shape', 'data_type', 'chunk_grid', 'codecs',
                                                  'fill_value', 'attributes') if key in meta}
        print(name, json.dumps(summary)[:800])
    try:
        import xarray as xr
        ds = xr.open_zarr(f'https://edh:{token}@{HOST}/era5/era5-land-v0.zarr', chunks={})
        longitude = -118.0 % 360 if float(ds.longitude.max()) > 180 else -118.0
        point = ds[['tp', 'e', 't2m']].sel(latitude=50.0, longitude=longitude, method='nearest')
        print(point.sel(valid_time=slice('2024-01-01T20', '2024-01-02T03')).load().to_dataframe().to_string())
    except Exception as error:
        print('point values skipped:', type(error).__name__, str(error).replace(token, '<token>'))


if __name__ == '__main__':
    main()
