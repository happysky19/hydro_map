"""Interrupted downloads must not produce a completed start or discard their retry input."""
from datetime import date
import json

import pytest

from catchment_forecast import aggregate, ifs, s2s, seas5


def write_grib(path, day, members=(0,)):
    eccodes = pytest.importorskip('eccodes')
    with path.open('wb') as stream:
        for number in members:
            handle = eccodes.codes_grib_new_from_samples('regular_ll_sfc_grib2')
            try:
                eccodes.codes_set(handle, 'dataDate', day)
                eccodes.codes_set(handle, 'dataTime', 0)
                eccodes.codes_set(handle, 'productDefinitionTemplateNumber', 1)
                eccodes.codes_set(handle, 'perturbationNumber', number)
                eccodes.codes_write(handle, stream)
            finally:
                eccodes.codes_release(handle)


def test_ifs_retries_a_start_with_only_one_finalized_member(monkeypatch, tmp_path):
    archive, output = tmp_path / 'archive', tmp_path / 'tables'
    folder = archive / '2026111500'
    folder.mkdir(parents=True)
    output.mkdir()
    (folder / 'ifs_c00.grib2').write_bytes(b'GRIBincomplete')

    def fetch(address, first=None, last=None):
        step = int(address.split('-')[-3].removesuffix('h'))
        entries, data = [], b''
        for kind, number in [('cf', 0), ('pf', 1)]:
            for param in ['2t', '2d', '10u', '10v'] + (['tp', 'ssrd'] if step == 24 else []):
                message = f'GRIB{number}:{param}:{step};'.encode()
                entries.append(dict(param=param, type=kind, number=number, _offset=len(data), _length=len(message)))
                data += message
        if address.endswith('.index'):
            return '\n'.join(json.dumps(item) for item in entries).encode()
        return data[first:last + 1]

    def write(system, start, catchments, target):
        names = sorted(path.name for path in start.glob('ifs_*.grib2'))
        (target / f'{start.name}.parquet').write_text(json.dumps(names))

    monkeypatch.setattr(ifs, 'fetch', fetch)
    monkeypatch.setattr(aggregate, 'run_start', write)
    ifs.run([date(2026, 11, 15)], archive, ['c00', 'p01'], hours=24, workers=2, output=output)
    assert json.loads((output / '2026111500.parquet').read_text()) == ['ifs_c00.grib2', 'ifs_p01.grib2']
    assert b'incomplete' not in (folder / 'ifs_c00.grib2').read_bytes()


def test_ifs_refuses_a_start_without_any_requested_member(monkeypatch, tmp_path):
    monkeypatch.setattr(ifs, 'fetch', lambda *args: b'')
    with pytest.raises(ValueError, match='no requested members'):
        ifs.download_start(date(2026, 11, 15), tmp_path, ['c00'], hours=24)
    assert not list(tmp_path.glob('*.grib2'))


def test_s2s_keeps_raw_input_until_aggregation_succeeds(monkeypatch, tmp_path):
    archive, output = tmp_path / 'archive', tmp_path / 'tables'
    raw = archive / 'raw' / '20261115_cf_pf.grib'
    raw.parent.mkdir(parents=True)
    output.mkdir()
    write_grib(raw, 20261115)
    monkeypatch.setattr(s2s, 'client', lambda *args: None)
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(aggregate, 'run_start', fail)
    with pytest.raises(OSError, match='disk full'):
        s2s.run([date(2026, 11, 15)], archive, 's2s-forecasts', output=output, delete=True)
    assert raw.exists()
    assert not (archive / 'done' / '20261115').exists()
    def write(system, folder, catchments, target):
        (target / f'{folder.name}.parquet').write_bytes(b'table')
    monkeypatch.setattr(aggregate, 'run_start', write)
    s2s.run([date(2026, 11, 15)], archive, 's2s-forecasts', output=output, delete=True)
    assert (output / '2026111500.parquet').exists()
    assert (archive / 'done' / '20261115').exists()
    assert not raw.exists() and not (archive / '2026111500').exists()


def test_seas5_resplits_retained_raw_after_partial_member_finalization(monkeypatch, tmp_path):
    archive, output = tmp_path / 'archive', tmp_path / 'tables'
    raw = archive / 'raw' / '2026110100.grib'
    raw.parent.mkdir(parents=True)
    folder = archive / '2026110100'
    folder.mkdir()
    output.mkdir()
    (folder / 'seas5_c00.grib').write_bytes(b'GRIBpartial')
    write_grib(raw, 20261101, members=(0, 1))
    monkeypatch.setattr(seas5, 'client', lambda *args: None)
    def write(system, start, catchments, target):
        names = sorted(path.name for path in start.glob('seas5_*.grib'))
        (target / f'{start.name}.parquet').write_text(json.dumps(names))
    monkeypatch.setattr(aggregate, 'run_start', write)
    seas5.run([date(2026, 11, 1)], archive, output=output, delete=True)
    assert json.loads((output / '2026110100.parquet').read_text()) == ['seas5_c00.grib', 'seas5_p001.grib']
    assert not raw.exists() and not folder.exists()


def test_s2s_download_only_can_be_aggregated_without_retrieval(monkeypatch, tmp_path):
    archive, output = tmp_path / 'archive', tmp_path / 'tables'
    raw = archive / 'raw' / '20261115_cf_pf.grib'
    raw.parent.mkdir(parents=True)
    output.mkdir()
    write_grib(raw, 20261115)
    monkeypatch.setattr(s2s, 'client', lambda *args: None)
    s2s.run([date(2026, 11, 15)], archive, 's2s-forecasts')
    def write(system, folder, catchments, target):
        (target / f'{folder.name}.parquet').write_bytes(b'table')
    monkeypatch.setattr(aggregate, 'run_start', write)
    s2s.run([date(2026, 11, 15)], archive, 's2s-forecasts', output=output, delete=True)
    assert (output / '2026111500.parquet').read_bytes() == b'table'
    assert not (archive / '2026111500').exists()


def test_s2s_reforecast_cache_distinguishes_requested_years(monkeypatch, tmp_path):
    monkeypatch.setattr(s2s, 'client', lambda *args: None)
    def retrieve(api, body, target, **kwargs):
        write_grib(target, int(body['hyear'][0]) * 10000 + 1115)
        return True
    monkeypatch.setattr(s2s, 'retrieve', retrieve)
    s2s.run([date(2026, 11, 15)], tmp_path, 's2s-reforecasts', years=range(2006, 2008))
    s2s.run([date(2026, 11, 15)], tmp_path, 's2s-reforecasts', years=range(2010, 2012))
    assert (tmp_path / '2006111500' / 'ec46_c00.grib').exists()
    assert (tmp_path / '2010111500' / 'ec46_c00.grib').exists()
