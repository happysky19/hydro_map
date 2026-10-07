"""Earth Data Hub responses: chunked Zarr v3 values in the CDS layout, each chunk fetched once."""

from datetime import date, datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

import numcodecs
import numpy as np

import edh_era5_land
from download_cds import accumulated_requests, make_requests, read_response


LATITUDE = np.round(50.4 - .1 * np.arange(8), 1)
LONGITUDE = np.round(239.6 + .1 * np.arange(8), 1)
FIRST = int((datetime(2025, 12, 30, tzinfo=timezone.utc) - edh_era5_land.EPOCH).total_seconds()) // 3600
DATA_CODECS = [{'name': 'numcodecs.bitround', 'configuration': {'keepbits': 13}},
               {'name': 'bytes', 'configuration': {'endian': 'little'}},
               {'name': 'blosc', 'configuration': {'typesize': 4, 'cname': 'zstd', 'clevel': 5,
                                                   'shuffle': 'shuffle', 'blocksize': 0}}]
AXIS_CODECS = [{'name': 'bytes', 'configuration': {'endian': 'little'}},
               {'name': 'zstd', 'configuration': {'level': 0, 'checksum': False}}]


def value(name, hour, row, column):
    """Temperature varies in space; precipitation accumulates from 1 mm at 01 UTC to 24 mm at 00 UTC."""
    return {'t2m': 270. + row + column / 10, 'tp': ((hour - 1) % 24 + 1) * 1e-3}.get(name, .25)


class Response:
    def __init__(self, content):
        self.content, self.status_code = content, 200 if content is not None else 404
        self.ok = content is not None

    def raise_for_status(self):
        pass


class FakeHub:
    """A small Zarr v3 store: 8 x 8 cells in 4 x 4 chunks, 48-hour time chunks, one chunk absent.

    Coordinates span several chunks, as long axes do in the real store.
    """
    def __init__(self):
        self.gets = []
        self.time_shape = FIRST + 72
        meta = lambda shape, chunks, dtype, codecs, **attributes: dict(
            shape=shape, data_type=dtype, codecs=codecs, attributes=attributes, fill_value='NaN',
            chunk_grid={'name': 'regular', 'configuration': {'chunk_shape': chunks}})
        self.arrays = {
            'latitude': meta([8], [8], 'float64', AXIS_CODECS), 'longitude': meta([8], [3], 'float64', AXIS_CODECS),
            'valid_time': meta([self.time_shape], [100000], 'int64', AXIS_CODECS, units='hours since 1950-01-01'),
            **{short: meta([self.time_shape, 8, 8], [48, 4, 4], 'float32', DATA_CODECS, units=units,
                           GRIB_stepType='accum' if short in edh_era5_land.LAND_ACCUMULATED else 'instant')
               for short, (_, units) in edh_era5_land.ALL_FIELDS.items()}}

    def axis(self, name, chunk):
        values = {'latitude': LATITUDE, 'longitude': LONGITUDE, 'valid_time': np.arange(self.time_shape)}[name]
        step, = self.arrays[name]['chunk_grid']['configuration']['chunk_shape']
        part = np.zeros(step, values.dtype)
        part[:len(values[chunk * step:(chunk + 1) * step])] = values[chunk * step:(chunk + 1) * step]
        return Response(numcodecs.Zstd().encode(part.astype('<' + values.dtype.str[1:]).tobytes()))

    def get(self, url, timeout):
        path = url[len(edh_era5_land.STORE) + 1:]
        self.gets.append(path)
        if path == 'zarr.json':
            return Response(json.dumps({'consolidated_metadata': {'metadata': self.arrays}}).encode())
        name, _, *index = path.split('/')
        if name in ('latitude', 'longitude', 'valid_time'):
            return self.axis(name, int(index[0]))
        t, y, x = map(int, index)
        if (y, x) == (1, 1):
            return Response(None)
        hours, rows, columns = np.meshgrid(t * 48 + np.arange(48), y * 4 + np.arange(4), x * 4 + np.arange(4),
                                           indexing='ij')
        block = np.vectorize(lambda h, r, c: value(name, h, r, c))(hours, rows, columns).astype('<f4')
        return Response(numcodecs.Blosc(cname='zstd', shuffle=1).encode(block.tobytes()))


class EdhTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.hub = FakeHub()
        self.client = edh_era5_land.EdhClient(self.root / 'chunks', session=self.hub)
        self.area = [50.4, -120.4, 49.7, -119.7]

    def test_states_and_endpoints_in_cds_layout(self):
        states = make_requests('era5-land', date(2025, 12, 30), self.area, date(2025, 12, 31))[0]
        self.client.retrieve(states['dataset'], states['request'], self.root / 'states.nc')
        fields = read_response(self.root / 'states.nc', states['fields'])
        t2m = fields['t2m']
        np.testing.assert_allclose(t2m['longitude'], -120.4 + .1 * np.arange(8), atol=1e-9)
        np.testing.assert_allclose(t2m['latitude'], LATITUDE, atol=1e-9)
        self.assertEqual(t2m['data'].shape, (48, 8, 8))
        np.testing.assert_allclose(t2m['data'][0, 2, 3], 270 + 2 + .3, rtol=1e-4)
        self.assertTrue(np.isnan(t2m['data'][:, 4:, 4:]).all())
        endpoints = accumulated_requests(date(2025, 12, 30), date(2025, 12, 31), self.area)
        for spec in endpoints:
            target = self.root / f"tp_{spec['request']['year']}.nc"
            self.client.retrieve(spec['dataset'], spec['request'], target)
            tp = read_response(target, spec['fields'])['tp']
            np.testing.assert_allclose(tp['data'][:, :4, :4], .024, rtol=1e-4)

    def test_hours_outside_the_store_are_refused(self):
        late = make_requests('era5-land', date(2026, 1, 2), self.area, date(2026, 1, 3))[0]
        with self.assertRaisesRegex(ValueError, 'outside'):
            self.client.retrieve(late['dataset'], late['request'], self.root / 'late.nc')

    def test_chunks_are_fetched_once_and_released(self):
        states = make_requests('era5-land', date(2025, 12, 30), self.area, date(2025, 12, 31))[0]
        for target in ('a.nc', 'b.nc'):
            self.client.retrieve(states['dataset'], states['request'], self.root / target)
        chunk_gets = [path for path in self.hub.gets if path.startswith('t2m/')]
        self.assertEqual(len(chunk_gets), len(set(chunk_gets)))
        self.assertTrue(any((self.root / 'chunks').iterdir()))
        self.client.release()
        self.assertFalse((self.root / 'chunks').exists())


if __name__ == '__main__':
    unittest.main()
