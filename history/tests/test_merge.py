"""Joining requested tables: earlier files win, later ones only extend the dates, and mismatches stop."""

from contextlib import redirect_stdout
import csv
from datetime import date, timedelta
import io
from pathlib import Path
import tempfile
import unittest

import pyarrow.parquet as pq

from catchment_history.export import _write
from catchment_history.merge import merge


HEADER = ['date', 'project_id', 'precipitation_mm', 'tmean_degC', 'aorc_gap_filled']


def table(path, first, last, value, projects=('A', 'B'), flag=None):
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    rows = [[day.isoformat(), project, None if day.day == 1 else value, value, flag]
            for day in days for project in projects]
    _write(path, path, HEADER, rows, text_columns={'aorc_gap_filled'})


class MergeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.old = self.root / 'old.parquet'
        table(self.old, date(2025, 11, 1), date(2025, 12, 31), 1.)

    def test_old_rows_kept_and_new_dates_appended(self):
        new = self.root / 'new.csv'
        table(new, date(2025, 12, 1), date(2026, 1, 31), 2., flag='precipitation_mm')
        output = self.root / 'merged.parquet'
        with redirect_stdout(io.StringIO()) as log:
            merge([self.old, new], output)
        self.assertIn('new.csv: 62 rows after 2025-12-31', log.getvalue())
        merged = pq.read_table(output)
        self.assertEqual(merged.column_names, HEADER)
        self.assertEqual(str(merged.schema.field('date').type), 'date32[day]')
        rows = merged.to_pylist()
        self.assertEqual(len(rows), 2 * (61 + 31))
        self.assertEqual(rows[0]['date'], date(2025, 11, 1))
        self.assertEqual(rows[-1]['date'], date(2026, 1, 31))
        december = [row for row in rows if row['date'] == date(2025, 12, 31)]
        self.assertEqual([row['tmean_degC'] for row in december], [1., 1.])
        self.assertIsNone(december[0]['aorc_gap_filled'])
        january = [row for row in rows if row['date'] == date(2026, 1, 1)]
        self.assertEqual([row['project_id'] for row in january], ['A', 'B'])
        self.assertIsNone(january[0]['precipitation_mm'])
        self.assertEqual(january[0]['aorc_gap_filled'], 'precipitation_mm')

    def test_the_first_file_can_be_extended_in_place(self):
        new = self.root / 'new.csv'
        table(new, date(2026, 1, 1), date(2026, 1, 3), 2.)
        with redirect_stdout(io.StringIO()):
            merge([self.old, new], self.old)
        dates = pq.read_table(self.old)['date'].to_pylist()
        self.assertEqual((dates[0], dates[-1], len(dates)), (date(2025, 11, 1), date(2026, 1, 3), 2 * (61 + 3)))

    def test_end_drops_the_later_dates(self):
        new = self.root / 'new.csv'
        table(new, date(2026, 1, 1), date(2026, 1, 31), 2.)
        output = self.root / 'until.parquet'
        with redirect_stdout(io.StringIO()):
            merge([self.old, new], output, end=date(2026, 1, 10))
            merge([output], self.root / 'cut.parquet', end=date(2025, 11, 30))
        self.assertEqual(max(pq.read_table(output)['date'].to_pylist()), date(2026, 1, 10))
        self.assertEqual(pq.read_table(self.root / 'cut.parquet')['date'].to_pylist(),
                         [date(2025, 11, 1) + timedelta(days=i // 2) for i in range(60)])

    def test_a_replaced_catchment_must_be_replaced_in_every_file(self):
        from unittest import mock
        new = self.root / 'new.csv'
        table(new, date(2026, 1, 1), date(2026, 1, 31), 2., flag='precipitation_mm')
        output = self.root / 'merged.parquet'
        with mock.patch('catchment_history.merge.REPLACED', ('A',)):
            with self.assertRaisesRegex(ValueError, 'A: AORC is replaced in new.csv only'):
                merge([self.old, new], output)
            self.assertFalse(output.exists())
            replaced = self.root / 'replaced.parquet'
            table(replaced, date(2025, 11, 1), date(2025, 12, 31), 1., flag='precipitation_mm')
            with redirect_stdout(io.StringIO()):
                merge([replaced, new], output)
        self.assertEqual(pq.read_table(output).num_rows, 2 * (61 + 31))

    def test_csv_output(self):
        new = self.root / 'new.parquet'
        table(new, date(2026, 1, 1), date(2026, 1, 2), 2.)
        output = self.root / 'merged.csv'
        with redirect_stdout(io.StringIO()):
            merge([self.old, new], output)
        with output.open() as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 2 * 63)
        self.assertEqual(rows[-1]['tmean_degC'], '2.0')
        self.assertEqual(rows[-1]['aorc_gap_filled'], '')

    def test_gap_columns_and_catchments_are_refused(self):
        cases = {
            'consecutive': dict(first=date(2026, 1, 2)),
            'columns differ': dict(header=[*HEADER[:2], 'tmax_degC', *HEADER[3:]]),
            'catchments': dict(projects=('A',)),
        }
        for message, case in cases.items():
            with self.subTest(message=message):
                new = self.root / f'{message}.parquet'
                days = [case.get('first', date(2026, 1, 1)) + timedelta(days=i) for i in range(3)]
                rows = [[day.isoformat(), project, 1., 1., None]
                        for day in days for project in case.get('projects', ('A', 'B'))]
                _write(new, new, case.get('header', HEADER), rows, text_columns={'aorc_gap_filled'})
                output = self.root / 'merged.parquet'
                with self.assertRaisesRegex(ValueError, message), redirect_stdout(io.StringIO()):
                    merge([self.old, new], output)
                self.assertFalse(output.exists())
                self.assertFalse(output.with_name('merged.parquet.part').exists())


if __name__ == '__main__':
    unittest.main()
