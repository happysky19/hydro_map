"""Exercise the real AORC/CDS daily contracts through the final source pivot."""

import csv
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from aggregate_daily import HOURLY_FIELDS, aggregate_daily, daily_schema
from download_aorc import BASE, FIELDS, accumulate, atomic_json, digest, load_polygons
from download_cds import json_hash, run_pipeline
from export_daily import export_daily
from meteorology import DERIVED_FIELDS, DERIVED_METHODS, derive_native, saturation_vapor_pressure_kpa
from test_download_cds import FakeClient


class DailyPipelineIntegrationTests(unittest.TestCase):
    def test_three_sources_cross_year_into_unique_project_days(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            geometry = root / 'catchments.geojson'
            geometry.write_text(json.dumps(dict(type='FeatureCollection', features=[
                dict(type='Feature', properties=dict(id=identifier, name=identifier, part='local'),
                     geometry=dict(type='Polygon', coordinates=[[
                         [west, 49.96], [west + .04, 49.96], [west + .04, 50.04],
                         [west, 50.04], [west, 49.96]]]))
                for identifier, west in [('A', -120.06), ('B', -119.98)]])))
            start, end = date(2025, 12, 31), date(2026, 1, 1)
            folders = []
            for product in ('era5-land', 'era5'):
                folder = root / product
                run_pipeline(geometry, product, folder, root / (product + '-cache'),
                             start, end, client=FakeClient())
                folders.append(folder)

            # Synthetic native inputs feed production diagnostics/area means and
            # aggregation; CDS synthetic NetCDF feeds its complete real pipeline.
            aorc = root / 'aorc'
            aorc.mkdir()
            polygons, forcing = load_polygons(geometry, None, include_metadata=True)
            latitude = {project: polygon.centroid.y for project, polygon in polygons.items()}
            definitions = {name: (units, kind) for name, units, kind in FIELDS.values()}
            definitions.update(DERIVED_FIELDS)
            run = dict(schema=2, source=BASE, source_id='aorc_v1.1', projects=sorted(polygons),
                       start=str(start), end=str(end), day='UTC', geometry_sha256=digest(geometry),
                       **forcing, variables=sorted(FIELDS), derive=True,
                       derived_methods=DERIVED_METHODS, pet_centroid_latitudes=latitude,
                       daily_schema=daily_schema(definitions, include_pet=True),
                       code_sha256={name: digest(Path(__file__).with_name(name)) for name in
                           ('download_aorc.py', 'aggregate_daily.py', 'meteorology.py')})
            atomic_json(aorc / 'run.json', run)
            hour = np.arange(49)
            temperature = np.array([-2., 3.])[None, :] + np.sin(hour[:, None] * 2 * np.pi / 24)
            pressure = np.full((49, 2), 90000.)
            saturation = saturation_vapor_pressure_kpa(temperature)
            humidity = .622 * saturation / (pressure / 1000 - .378 * saturation)
            hourly_rows = []
            first = datetime.combine(start, datetime.min.time(), timezone.utc)
            for project, multiplier in [('A', 1), ('B', 2)]:
                cells = dict(temperature_c=temperature, specific_humidity_kgkg=humidity,
                    surface_pressure_pa=pressure,
                    precipitation_mm=np.array([2., 4.])[None, :] * multiplier
                        * np.where(hour[:, None] <= 24, 1., 2.),
                    u_wind_ms=np.tile([3., -3.], (49, 1)),
                    v_wind_ms=np.tile([4., -4.], (49, 1)),
                    shortwave_down_wm2=np.full((49, 2), 100.),
                    longwave_down_wm2=np.full((49, 2), 300.))
                cells.update(derive_native(cells))
                for name, (units, kind) in definitions.items():
                    values, fractions = accumulate(cells[name], np.array([.5, .5]))
                    selected = range(1, 49) if kind == 'hour_ending_amount' else range(48)
                    for index in selected:
                        hourly_rows.append(dict(source='aorc_v1.1', project_id=project,
                            time_utc=(first + timedelta(hours=index)).isoformat(), variable=name,
                            value=values[index], units=units, temporal_kind=kind,
                            valid_area_fraction=fractions[index], qc='valid'))
            hourly_rows.sort(key=lambda row: (row['source'], row['project_id'], row['variable'], row['time_utc']))
            hourly = root / 'aorc_hourly.csv'
            with hourly.open('w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=HOURLY_FIELDS)
                writer.writeheader()
                writer.writerows(hourly_rows)
            for day in (start, end):
                daily = aorc / f'daily_{day.year}.csv.gz'
                count = aggregate_daily(hourly, daily, day, day, pet_latitudes=latitude)
                atomic_json(aorc / f'year_{day.year}.json', dict(
                    configuration_sha256=json_hash(run), daily_sha256=digest(daily),
                    daily_rows=count, status='computed_all_hours_valid'))
            folders.insert(0, aorc)

            output = root / 'delivery.csv'
            report = export_daily(geometry, folders, start, end, output)
            with output.open(newline='') as stream:
                rows = list(csv.DictReader(stream))
            with (root/'delivery_qc.csv').open(newline='') as stream:
                quality = list(csv.DictReader(stream))
            self.assertEqual([(row['date'], row['project_id']) for row in rows],
                             [(str(day), project) for day in (start, end) for project in ('A', 'B')])
            self.assertEqual(report['row_count'], 4)
            self.assertEqual(len(rows[0]), 47)
            self.assertEqual(len(quality[0]), 182)
            self.assertEqual(len(rows), len(quality))
            for row, qc in zip(rows, quality):
                self.assertEqual((row['date'], row['project_id']), (qc['date'], qc['project_id']))
                multiplier = (1 if row['project_id'] == 'A' else 2) * (1 if row['date'] == str(start) else 2)
                precipitation = float(row['aorc_v1_1__precipitation_mm'])
                rain, snow = (float(row['aorc_v1_1__' + name]) for name in ('rainfall_mm', 'snowfall_mm'))
                self.assertAlmostEqual(precipitation, 72. * multiplier)
                self.assertAlmostEqual(rain, 48. * multiplier)
                self.assertAlmostEqual(snow, 24. * multiplier)
                self.assertAlmostEqual(rain + snow, precipitation)
                self.assertAlmostEqual(float(row['era5_land_cds__precipitation_mm']), 2.)
                self.assertEqual(qc['era5_land_cds__precipitation_mm__valid_hours'], '24')
                self.assertAlmostEqual(float(row['aorc_v1_1__tmean_degC']), .5)
                self.assertAlmostEqual(float(row['era5_land_cds__tmean_degC']), 6.85)
                self.assertAlmostEqual(float(row['aorc_v1_1__wind_speed_m_s']), 5.)
                self.assertAlmostEqual(float(row['era5_cds__cloud_cover_fraction']), .5)
                self.assertAlmostEqual(float(row['era5_cds__freezing_level_geopotential_height_m']),
                                       (280 - 273.15) / .006)
                self.assertTrue(all(value == 'valid' for key, value in qc.items() if key.endswith('__qc')))
            manifest = json.loads(Path(str(output) + '.manifest.json').read_text())
            self.assertEqual(manifest['output_sha256'], digest(output))
            self.assertEqual(manifest['geometry_sha256'], digest(geometry))
            self.assertEqual(manifest['columns']['aorc_v1_1__pet_hargreaves_mm_day']['units'], 'mm/day')
            self.assertEqual(manifest['columns']['aorc_v1_1__tmin_degC']['statistic'],
                             'minimum_of_hourly_catchment_means')
            self.assertEqual(manifest['columns']['era5_land_cds__precipitation_mm']['units'], 'mm')
            self.assertEqual(manifest['columns']['era5_cds__cloud_cover_fraction']['units'], '1')
            self.assertEqual({entry['metadata']['source_id'] for entry in manifest['inputs']},
                             {'aorc_v1.1', 'era5_land_cds', 'era5_cds'})
            self.assertTrue(all(len(entry['periods']) == 2 for entry in manifest['inputs']))
            self.assertEqual(manifest['inputs'][0]['metadata']['derived_methods'], DERIVED_METHODS)


if __name__ == '__main__':
    unittest.main()
