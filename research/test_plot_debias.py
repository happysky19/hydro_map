"""The comparison figures are written from AORC and ERA5-Land tables, with one PDF page per catchment."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest

import plot_debias
from test_debias_era5 import LATITUDES, write_inputs


class PlotDebiasTests(unittest.TestCase):
    def test_figures_are_written(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inputs = write_inputs(root)
            with redirect_stdout(io.StringIO()):
                plot_debias.run(inputs['aorc_path'], inputs['era5_path'], root / 'figures')
            for name in ('timeseries.png', 'monthly_bias.png', 'quantiles.png', 'catchments.png'):
                self.assertGreater((root / 'figures' / name).stat().st_size, 10000, name)
            pdf = (root / 'figures' / 'catchments.pdf').read_bytes()
            self.assertEqual(pdf.count(b'/Type /Page\n') + pdf.count(b'/Type /Page '), len(LATITUDES))


if __name__ == '__main__':
    unittest.main()
