"""The comparison figures are written from AORC and ERA5-Land tables, with one PDF page per catchment."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from catchment_history import debias, plot_debias
from test_debias import LATITUDES, patch_settings, write_inputs


class PlotDebiasTests(unittest.TestCase):
    def test_figures_are_written(self):
        # The synthetic years, 2022-2025, all count as AORC years here.
        patch_settings(self, REPLACED_FROM='2030-01-01', RECENT=('2022-01-01', '2025-12-31'))
        patcher = mock.patch.object(plot_debias, 'REPLACED_FROM', '2030-01-01')
        patcher.start()
        self.addCleanup(patcher.stop)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inputs = write_inputs(root)
            with redirect_stdout(io.StringIO()):
                plot_debias.run(inputs['aorc_path'], inputs['era5_path'], root / 'figures')
            self.assertIn('precipitation_mm,ALL,', (root / 'figures' / 'check.csv').read_text())
            for name in ('timeseries.png', 'monthly_bias.png', 'quantiles.png', 'catchments.png'):
                self.assertGreater((root / 'figures' / name).stat().st_size, 10000, name)
            pdf = (root / 'figures' / 'catchments.pdf').read_bytes()
            self.assertEqual(pdf.count(b'/Type /Page\n') + pdf.count(b'/Type /Page '), len(LATITUDES))


if __name__ == '__main__':
    unittest.main()
