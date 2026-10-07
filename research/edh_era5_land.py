"""Answer ERA5-Land CDS requests from the DestinE Earth Data Hub Zarr store.

The Earth Data Hub keeps the hourly 0.1 degree ERA5-Land fields as Zarr v3
arrays chunked by 60 days x 5 x 10 degrees, with GRIB values and accumulation
conventions. The client writes the NetCDF layout that a CDS request returns, so
caching, resumption and daily processing are unchanged. Raw chunks are kept in
a disk cache while the requests of one year are answered, because consecutive
batches share chunks, and released afterwards.

The store has gaps (surface net thermal radiation is blank everywhere from
2024-11-01 to 2024-11-27); the pipeline replaces any response that is blank in
a catchment cell with the CDS original.

Access needs a free DestinE account; the personal access token is read from
~/.netrc (machine data.earthdatahub.destine.eu password <token>).
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import netrc
from pathlib import Path
import shutil
import threading
import time

import netCDF4
import numcodecs
import numpy as np
import requests

from arco_era5 import listed, request_times
from cds_fields import ALL_FIELDS, LAND_ACCUMULATED, NETCDF_LOCK


HOST = 'data.earthdatahub.destine.eu'
STORE = f'https://{HOST}/era5/era5-land-v0.zarr'
EPOCH = datetime(1950, 1, 1, tzinfo=timezone.utc)
NAMES = {long: short for short, (long, _) in ALL_FIELDS.items()}


def token():
    try:
        return netrc.netrc(Path.home() / '.netrc').authenticators(HOST)[2]
    except (FileNotFoundError, TypeError):
        raise ValueError(f'Add "machine {HOST} password <token>" to ~/.netrc for the Earth Data Hub')


class EdhClient:
    """Drop-in replacement for cdsapi.Client.retrieve on ERA5-Land requests."""
    provider = 'DestinE Earth Data Hub ERA5-Land (era5-land-v0.zarr)'

    def __init__(self, chunk_dir, timeout=120, attempts=5, workers=8, session=None):
        self.chunk_dir, self.timeout, self.attempts, self.workers = Path(chunk_dir), timeout, attempts, workers
        self.session = session or requests.Session()
        if session is None:
            self.session.auth = ('edh', token())
        self.lock, self.pending, self.absent = threading.Lock(), {}, set()
        self.verbose = True
        self.metadata = None

    def _get(self, path):
        for attempt in range(self.attempts):
            try:
                response = self.session.get(f'{STORE}/{path}', timeout=self.timeout)
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                return response.content
            except requests.RequestException:
                if attempt == self.attempts - 1:
                    raise
                time.sleep(2 ** attempt)

    def _axis(self, arrays, name):
        """A whole one-dimensional coordinate, which may span several chunks."""
        meta = arrays[name]
        length, = meta['shape']
        step, = meta['chunk_grid']['configuration']['chunk_shape']
        parts = [self._decode(meta, self._get(f'{name}/{self._key(meta, (i,))}')) for i in range(-(-length // step))]
        return np.concatenate(parts)[:length]

    def _load_metadata(self):
        with self.lock:
            if self.metadata is not None:
                return
        root = self._get('zarr.json')
        if root is None:
            raise ValueError('Earth Data Hub ERA5-Land store not found')
        arrays = json.loads(root)['consolidated_metadata']['metadata']
        units = arrays['valid_time'].get('attributes', {}).get('units', 'hours since 1950-01-01')
        if not units.startswith('hours since 1950-01-01'):
            raise ValueError(f'Unexpected Earth Data Hub time units: {units}')
        coordinates = {name: self._axis(arrays, name) for name in ('latitude', 'longitude', 'valid_time')}
        with self.lock:
            self.arrays, self.coordinates, self.metadata = arrays, coordinates, True

    @staticmethod
    def _key(meta, index):
        encoding = meta.get('chunk_key_encoding', {'name': 'default', 'configuration': {'separator': '/'}})
        separator = encoding.get('configuration', {}).get('separator', '/')
        parts = [str(i) for i in index]
        return 'c' + separator + separator.join(parts) if encoding['name'] == 'default' else '.'.join(parts)

    @staticmethod
    def _decode(meta, payload):
        """Undo the codec pipeline; bitround only drops precision when encoding."""
        shape = meta['chunk_grid']['configuration']['chunk_shape']
        dtype = {'float32': '<f4', 'float64': '<f8', 'int64': '<i8'}[meta['data_type']]
        if payload is None:
            return np.full(shape, np.nan, dtype=dtype)
        for codec in reversed(meta['codecs']):
            name = codec['name']
            if name == 'blosc':
                payload = numcodecs.Blosc().decode(payload)
            elif name == 'zstd':
                payload = numcodecs.Zstd().decode(payload)
            elif name == 'bytes':
                if codec.get('configuration', {}).get('endian', 'little') != 'little':
                    raise ValueError('Unsupported big-endian Earth Data Hub chunk')
            elif name not in ('numcodecs.bitround',):
                raise ValueError(f'Unsupported Earth Data Hub codec: {name}')
        return np.frombuffer(payload, dtype=dtype).reshape(shape)

    def _fetch(self, short, index):
        """Path of a raw chunk on disk, downloaded once even when threads ask together."""
        key = f'{short}/{self._key(self.arrays[short], index)}'
        path = self.chunk_dir / hashlib.sha256(key.encode()).hexdigest()
        while True:
            with self.lock:
                if path.exists():
                    return path
                event = self.pending.get(path)
                if event is None:
                    event = self.pending[path] = threading.Event()
                    break
            # Another thread is downloading it; if that fails, try again here.
            event.wait()
        try:
            payload = self._get(key)
            if payload is None:
                # The store omits chunks without data; over land this would leave blanks.
                if self.verbose:
                    print(f'Earth Data Hub has no chunk {key}; its cells are blank', flush=True)
                with self.lock:
                    self.absent.add(key)
            self.chunk_dir.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix('.part')
            temporary.write_bytes(b'' if payload is None else payload)
            temporary.replace(path)
            return path
        finally:
            with self.lock:
                self.pending.pop(path).set()

    def release(self):
        """Delete cached raw chunks once their requests have been answered."""
        shutil.rmtree(self.chunk_dir, ignore_errors=True)

    def _positions(self, stamps):
        """Indexes of the requested hours on the store's time axis."""
        hours = np.array([int((stamp - EPOCH).total_seconds()) // 3600 for stamp in stamps])
        valid_time = self.coordinates['valid_time']
        positions = hours - int(valid_time[0])
        if (positions.min() < 0 or positions.max() >= len(valid_time)
                or not np.array_equal(valid_time[positions], hours)):
            raise ValueError('Requested hours are outside the Earth Data Hub ERA5-Land time axis')
        return positions

    def _blocks(self, short, positions, rows, columns):
        """Chunk indexes and the requested positions, rows and columns each one holds."""
        chunk_t, chunk_y, chunk_x = self.arrays[short]['chunk_grid']['configuration']['chunk_shape']
        for t in np.unique(positions // chunk_t):
            at = positions // chunk_t == t
            for y in np.unique(rows // chunk_y):
                ay = rows // chunk_y == y
                for x in np.unique(columns // chunk_x):
                    ax = columns // chunk_x == x
                    yield (int(t), int(y), int(x)), (at, ay, ax), (positions[at] % chunk_t, rows[ay] % chunk_y,
                                                                    columns[ax] % chunk_x)

    def series(self, short, positions, rows, columns):
        """Values at time positions and grid rows/columns, shape (time, rows, columns)."""
        result = np.full((len(positions), len(rows), len(columns)), np.nan, dtype='<f4')
        for index, target, source in self._blocks(short, positions, rows, columns):
            payload = self._fetch(short, index).read_bytes()
            result[np.ix_(*target)] = self._decode(self.arrays[short], payload or None)[np.ix_(*source)]
        return result

    def grid(self, area):
        """Rows and west-to-east columns inside a CDS area [north, west, south, east]."""
        north, west, south, east = area
        latitude, longitude = self.coordinates['latitude'], self.coordinates['longitude']
        rows = np.flatnonzero((latitude <= north + 1e-6) & (latitude >= south - 1e-6))
        wrapped = (longitude + 180) % 360 - 180
        columns = np.flatnonzero((wrapped >= west - 1e-6) & (wrapped <= east + 1e-6))
        return rows, columns[np.argsort(wrapped[columns])], wrapped

    def retrieve(self, dataset, request, target):
        if dataset != 'reanalysis-era5-land':
            raise ValueError(f'The Earth Data Hub client serves ERA5-Land only, not {dataset}')
        self._load_metadata()
        unknown = [name for name in listed(request['variable']) if NAMES.get(name) not in self.arrays]
        if unknown:
            raise ValueError(f'Not available from the Earth Data Hub: {unknown}')
        names = [NAMES[name] for name in listed(request['variable'])]
        stamps = request_times(request)
        positions = self._positions(stamps)
        rows, columns, wrapped = self.grid(request['area'])
        needed = [(short, index) for short in names for index, _, _ in self._blocks(short, positions, rows, columns)]
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            list(pool.map(lambda item: self._fetch(*item), needed))
        values = {short: self.series(short, positions, rows, columns) for short in names}
        with NETCDF_LOCK, netCDF4.Dataset(target, 'w') as ds:
            ds.createDimension('valid_time', len(stamps))
            ds.createDimension('latitude', len(rows))
            ds.createDimension('longitude', len(columns))
            valid_time = ds.createVariable('valid_time', 'i8', ('valid_time',))
            valid_time.units, valid_time.calendar = 'seconds since 1970-01-01', 'proleptic_gregorian'
            valid_time[:] = [int(stamp.timestamp()) for stamp in stamps]
            ds.createVariable('latitude', 'f8', ('latitude',))[:] = self.coordinates['latitude'][rows]
            ds.createVariable('longitude', 'f8', ('longitude',))[:] = wrapped[columns]
            for short in names:
                # The store's own units and step type, so the usual response checks apply to them.
                attributes = self.arrays[short].get('attributes', {})
                variable = ds.createVariable(short, 'f4', ('valid_time', 'latitude', 'longitude'),
                                             zlib=True, complevel=1)
                variable.units = attributes.get('units', ALL_FIELDS[short][1])
                variable.GRIB_stepType = attributes.get('GRIB_stepType',
                                                        'accum' if short in LAND_ACCUMULATED else 'instant')
                variable[:] = values[short]
            ds.source = STORE
