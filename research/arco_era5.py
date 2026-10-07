"""Answer ERA5 single-level CDS requests from the public ARCO-ERA5 Zarr store.

Google's Analysis-Ready, Cloud-Optimized ERA5 holds the same hourly 0.25 degree
fields as the Copernicus CDS, one global hour per chunk, with no request queue.
The client writes the NetCDF layout that a CDS request returns, so caching,
resumption and daily processing are unchanged and a response from either
provider can stand in for the other.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import threading
import time

import netCDF4
import numcodecs
import numpy as np
import requests


STORE = 'https://storage.googleapis.com/gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3'
# CDS request name -> (NetCDF name, ARCO array, units)
FIELDS = {'total_cloud_cover': ('tcc', 'total_cloud_cover', '(0 - 1)'),
          'zero_degree_level': ('deg0l', 'zero_degree_level', 'm'),
          'geopotential': ('z', 'geopotential_at_surface', 'm**2 s**-2')}
# Surface geopotential is constant in time; one hour per request is enough.
STATIC = {'geopotential'}
EPOCH = datetime(1900, 1, 1, tzinfo=timezone.utc)
TIME_CHUNK = 67706
LATITUDE = 90 - .25 * np.arange(721)
LONGITUDE = .25 * np.arange(1440)


def listed(value):
    return value if isinstance(value, list) else [value]


def request_times(request):
    """Valid UTC hours of a CDS request: the product of years, months, days and times."""
    stamps = []
    for year in listed(request['year']):
        for month in listed(request['month']):
            for day in listed(request['day']):
                for hour in listed(request['time']):
                    try:
                        stamps.append(datetime(int(year), int(month), int(day), int(hour[:2]), tzinfo=timezone.utc))
                    except ValueError:
                        continue
    return sorted(stamps)


def area_indexes(area):
    """Grid rows and columns inside a CDS area [north, west, south, east], west to east."""
    north, west, south, east = area
    rows = np.flatnonzero((LATITUDE <= north + 1e-9) & (LATITUDE >= south - 1e-9))
    longitude = (LONGITUDE + 180) % 360 - 180
    columns = np.flatnonzero((longitude >= west - 1e-9) & (longitude <= east + 1e-9))
    return rows, columns[np.argsort(longitude[columns])], longitude


class ArcoClient:
    """Drop-in replacement for cdsapi.Client.retrieve on ERA5 single-level requests."""
    provider = 'Google ARCO-ERA5 (gcp-public-data-arco-era5)'

    def __init__(self, workers=16, timeout=120, attempts=4):
        self.workers, self.timeout, self.attempts = workers, timeout, attempts
        self.session = requests.Session()
        self.codec = numcodecs.Blosc()
        self.checked, self.lock = set(), threading.Lock()

    def _get(self, path):
        for attempt in range(self.attempts):
            try:
                response = self.session.get(f'{STORE}/{path}', timeout=self.timeout)
                if response.status_code == 404:
                    raise FileNotFoundError(f'{STORE}/{path}')
                response.raise_for_status()
                return self.codec.decode(response.content)
            except requests.RequestException:
                if attempt == self.attempts - 1:
                    raise
                time.sleep(2 ** attempt)

    def _check_time(self, index):
        chunk = index // TIME_CHUNK
        with self.lock:
            if chunk in self.checked:
                return
        values = np.frombuffer(self._get(f'time/{chunk}'), '<i8')
        if values[index % TIME_CHUNK] != index:
            raise ValueError('Unexpected ARCO-ERA5 time axis')
        with self.lock:
            self.checked.add(chunk)

    def _field(self, array, index, rows, columns):
        data = np.frombuffer(self._get(f'{array}/{index}.0.0'), '<f4').reshape(len(LATITUDE), len(LONGITUDE))
        return data[np.ix_(rows, columns)]

    def retrieve(self, dataset, request, target):
        if dataset != 'reanalysis-era5-single-levels':
            raise ValueError(f'ARCO-ERA5 serves ERA5 single levels only, not {dataset}')
        unknown = set(request['variable']) - set(FIELDS)
        if unknown:
            raise ValueError(f'Not available from ARCO-ERA5: {sorted(unknown)}')
        stamps = request_times(request)
        indexes = [int((stamp - EPOCH).total_seconds()) // 3600 for stamp in stamps]
        for index in sorted({index // TIME_CHUNK * TIME_CHUNK for index in indexes}):
            self._check_time(index)
        rows, columns, longitude = area_indexes(request['area'])
        fields = {}
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for name in request['variable']:
                short, array, units = FIELDS[name]
                if name in STATIC:
                    field = self._field(array, indexes[0], rows, columns)
                    data = np.broadcast_to(field, (len(indexes), *field.shape))
                else:
                    data = np.stack(list(pool.map(lambda index: self._field(array, index, rows, columns), indexes)))
                fields[short] = (data, units)
        with netCDF4.Dataset(target, 'w') as ds:
            ds.createDimension('valid_time', len(stamps))
            ds.createDimension('latitude', len(rows))
            ds.createDimension('longitude', len(columns))
            valid_time = ds.createVariable('valid_time', 'i8', ('valid_time',))
            valid_time.units, valid_time.calendar = 'seconds since 1970-01-01', 'proleptic_gregorian'
            valid_time[:] = [int(stamp.timestamp()) for stamp in stamps]
            ds.createVariable('latitude', 'f8', ('latitude',))[:] = LATITUDE[rows]
            ds.createVariable('longitude', 'f8', ('longitude',))[:] = longitude[columns]
            for short, (data, units) in fields.items():
                variable = ds.createVariable(short, 'f4', ('valid_time', 'latitude', 'longitude'),
                                             zlib=True, complevel=1)
                variable.units, variable.GRIB_stepType = units, 'instant'
                variable[:] = data
            ds.source = STORE
