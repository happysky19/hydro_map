"""The progress report counts downloaded requests and processed periods without changing files."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import progress_daily
from download_cds import run_pipeline
from test_download_cds import FakeClient


class ProgressTests(unittest.TestCase):
    def test_reports_each_source(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            geometry = work / 'catchments.geojson'
            geometry.write_text(json.dumps({'type': 'FeatureCollection', 'features': [{
                'type': 'Feature', 'properties': {'id': 'A', 'name': 'A', 'part': 'local'},
                'geometry': {'type': 'Polygon', 'coordinates': [[[-120.03, 49.97], [-119.97, 49.97],
                              [-119.97, 50.03], [-120.03, 50.03], [-120.03, 49.97]]]}}]}))
            with redirect_stdout(io.StringIO()):
                run_pipeline(geometry, 'era5', work / 'era5', work / 'cache/cds', '2025-12-31', '2025-12-31',
                             client=FakeClient())
                run_pipeline(geometry, 'era5-land', work / 'era5-land', work / 'cache/cds', '2025-12-31',
                             '2025-12-31', client=FakeClient())
            (work / 'aorc').mkdir()
            (work / 'aorc/run.json').write_text(json.dumps({'start': '2024-01-01', 'end': '2025-12-31'}))
            (work / 'aorc/year_2025.json').write_text('{}')
            output = io.StringIO()
            with patch.object(sys, 'argv', ['progress_daily.py', str(work)]), redirect_stdout(output):
                progress_daily.main()
            lines = output.getvalue().splitlines()
            self.assertIn('1/2 years processed (2025-2025)', lines[0])
            self.assertIn('2/2 requests downloaded, 1/1 months processed', lines[1])
            self.assertIn('from CDS 2', lines[1])
            self.assertIn('1/1 requests downloaded, 1/1 months processed', lines[2])


if __name__ == '__main__':
    unittest.main()
