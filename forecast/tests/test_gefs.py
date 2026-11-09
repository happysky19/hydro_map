from datetime import date

import pytest

from catchment_forecast import gefs


def test_members_and_starts():
    assert gefs.members(31)[:3] == ['c00', 'p01', 'p02'] and gefs.members(31)[-1] == 'p30'
    assert len(gefs.members(21)) == 21
    days = gefs.starts(date(2024, 2, 1), date(2024, 3, 5))
    assert [d.day for d in days if d.month == 2] == [1, 5, 9, 13, 17, 21, 25]
    assert days[-2:] == [date(2024, 3, 1), date(2024, 3, 5)]
    assert len(gefs.starts(date(2024, 2, 27), date(2024, 3, 2), every_day=True)) == 5
    assert gefs.url(date(2024, 1, 5), 'c00', 'a', 6).endswith(
        'gefs.20240105/00/atmos/pgrb2ap5/gec00.t00z.pgrb2a.0p50.f006')
    assert '/pgrb2bp5/gep07.t00z.pgrb2b.0p50.f528' in gefs.url(date(2024, 1, 5), 'p07', 'b', 528)


def inventory(names, size=10):
    """(file bytes, wgrib2 index) of messages named VARIABLE:level, each GRIB plus padding to size bytes."""
    data, lines = b'', []
    for number, name in enumerate(names, 1):
        lines.append(f'{number}:{len(data)}:d=2024010500:{name}:6 hour fcst:')
        data += (b'GRIB' + name.encode())[:size].ljust(size, b'.')
    return data, '\n'.join(lines) + '\n'


def test_ranges_take_the_wanted_messages_and_merge_neighbours(monkeypatch):
    _, index = inventory(['TMP:2 m above ground', 'RH:2 m above ground', 'APCP:surface', 'PWAT:x', 'SNOD:surface'])
    monkeypatch.setattr(gefs, 'GAP', 5)
    found, names = gefs.ranges(index, {'TMP:2 m above ground', 'APCP:surface', 'SNOD:surface'})
    assert names == {'TMP:2 m above ground', 'APCP:surface', 'SNOD:surface'}
    assert found == [(0, 9), (20, 29), (40, None)]
    monkeypatch.setattr(gefs, 'GAP', 15)
    assert gefs.ranges(index, {'TMP:2 m above ground', 'APCP:surface'})[0] == [(0, 29)]


class Archive:
    """A fake bucket: every file holds the wanted fields and one other, except those named in missing."""

    def __init__(self, missing=()):
        self.missing, self.requests = set(missing), 0

    def files(self, address):
        kind, step = address.split('pgrb2')[2][0], int(address[-3:])
        names = list(gefs.FIELDS[kind]) + list(gefs.ONCE.get(kind, ())) + ['PWAT:entire atmosphere']
        names = [name for name in names if (step or name not in gefs.PERIOD) and name not in self.missing]
        return inventory([f'{name}' for name in names], size=40)

    def fetch(self, address, first=None, last=None):
        self.requests += 1
        if '20240109' in address:
            raise FileNotFoundError(address)
        data, index = self.files(address.removesuffix('.idx'))
        if address.endswith('.idx'):
            return index.encode()
        return data[first:None if last is None else last + 1]


def test_a_member_file_holds_every_step_of_the_wanted_fields(monkeypatch, tmp_path):
    archive = Archive()
    monkeypatch.setattr(gefs, 'fetch', archive.fetch)
    size = gefs.download_member(date(2024, 1, 5), 'p01', tmp_path, workers=4, hours=12)
    data = (tmp_path / 'gefs_p01.grib2').read_bytes()
    assert size == len(data) and not list(tmp_path.glob('*.part'))
    messages = data.split(b'GRIB')[1:]
    wanted = len(gefs.FIELDS['a']) + len(gefs.FIELDS['b'])
    # Step 0 has no 6 h fields; HGT:surface comes at step 6 only; PWAT comes where it lies between wanted fields.
    assert sum(not m.startswith(b'PWAT') for m in messages) == 3 * wanted - len(gefs.PERIOD) + 1
    assert gefs.download_member(date(2024, 1, 5), 'p01', tmp_path, workers=4, hours=12) == 0


def test_weather_fetches_only_the_fields_the_product_uses(monkeypatch, tmp_path):
    monkeypatch.setattr(gefs, 'fetch', Archive().fetch)
    gefs.download_member(date(2024, 1, 5), 'c00', tmp_path, workers=2, hours=12, weather=True)
    messages = [m.rstrip(b'.').decode() for m in (tmp_path / 'gefs_c00.grib2').read_bytes().split(b'GRIB')[1:]]
    assert set(messages) == set(gefs.WEATHER_FIELDS['a']) | set(gefs.WEATHER_FIELDS['b'])
    # Step 0 has no APCP or DSWRF, and HGT:surface is not fetched.
    assert len(messages) == 3 * 6 - 2


def test_a_field_missing_from_the_archive_stops_the_member(monkeypatch, tmp_path):
    monkeypatch.setattr(gefs, 'fetch', Archive(missing={'SNOHF:surface'}).fetch)
    with pytest.raises(ValueError, match='no SNOHF:surface'):
        gefs.download_member(date(2024, 1, 5), 'c00', tmp_path, workers=2, hours=12)
    assert not list(tmp_path.iterdir())


def test_main_passes_the_hours_and_members_on(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(gefs, 'run', lambda days, archive, names, workers, hours, *rest:
                        seen.update(days=days, names=names, hours=hours, weather=rest[-1]))
    gefs.main(['--start', '2024-01-01', '--end', '2024-01-03', '--archive', str(tmp_path), '--every-day',
               '--members', '3', '--hours', '24'])
    assert seen['hours'] == 24 and seen['names'] == ['c00', 'p01', 'p02'] and len(seen['days']) == 3
    assert seen['weather'] is False
    gefs.main(['--start', '2024-01-01', '--end', '2024-01-01', '--archive', str(tmp_path), '--weather'])
    assert seen['weather'] is True and len(seen['days']) == 1
    with pytest.raises(SystemExit):
        gefs.main(['--start', '2024-01-01', '--end', '2024-01-01', '--archive', str(tmp_path), '--hours', '30'])


def test_run_skips_missing_starts_aggregates_and_deletes(monkeypatch, tmp_path, capsys):
    archive = Archive()
    monkeypatch.setattr(gefs, 'fetch', archive.fetch)
    monkeypatch.setattr(gefs, 'HOURS', 6)
    written = []

    def run_start(system, folder, catchments, output):
        assert system == 'gefs' and len(list(folder.glob('gefs_*.grib2'))) == 2
        written.append(folder.name)
        (output / f'{folder.name}.parquet').write_text('table')

    monkeypatch.setattr('catchment_forecast.aggregate.run_start', run_start)
    output = tmp_path / 'tables'
    output.mkdir()
    days = [date(2024, 1, 5), date(2024, 1, 9)]
    gefs.run(days, tmp_path / 'archive', gefs.members(2), 2, 6, catchments=None, output=output, delete=True)
    assert written == ['2024010500'] and 'not in the archive' in capsys.readouterr().out
    assert not (tmp_path / 'archive' / '2024010500').exists()
    requests = archive.requests
    gefs.run(days[:1], tmp_path / 'archive', gefs.members(2), 2, 6, catchments=None, output=output, delete=True)
    assert archive.requests == requests and written == ['2024010500']
