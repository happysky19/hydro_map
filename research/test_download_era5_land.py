"""Check forecast resets, missing predecessors and catalog boundary handling."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from download_era5_land import catalog_segments, deaccumulate_precipitation, main, merge_segments, request_windows, validate_metadata


class HourlyEra5LandTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2025,12,29,tzinfo=timezone.utc)
        self.times = np.array([(self.start+timedelta(hours=i)).timestamp() for i in range(73)])

    def test_three_daily_cycles_reset_at_01_and_end_at_next_00(self):
        cumulative = np.array([24 if i%24 == 0 else i%24 for i in range(73)],dtype=float)[:,None,None]/1000
        hourly, negative, missing = deaccumulate_precipitation(cumulative,self.times)
        self.assertTrue(np.isnan(hourly[0]).all())
        np.testing.assert_allclose(hourly[1:],1)
        np.testing.assert_allclose(hourly[1:].reshape(3,24).sum(axis=1),24)
        self.assertFalse(negative.any())
        self.assertEqual(np.flatnonzero(missing).tolist(),[0])
        self.assertEqual(datetime.fromtimestamp(self.times[-1],timezone.utc).isoformat(),'2026-01-01T00:00:00+00:00')

    def test_missing_previous_hour_is_not_bridged_and_reset_recovers(self):
        times = self.times[[1,3,25]]
        hourly, _, missing = deaccumulate_precipitation(np.array([1.,3.,2.])[:,None]/1000,times)
        np.testing.assert_allclose(hourly[[0,2],0],[1,2])
        self.assertTrue(np.isnan(hourly[1,0]))
        self.assertEqual(missing.tolist(),[False,True,False])

    def test_missing_cells_and_material_negative_differences_stay_invalid(self):
        data = np.array([[1,1,np.nan],[.99995,.9998,2]])/1000
        hourly, negative, _ = deaccumulate_precipitation(data,self.times[1:3])
        self.assertEqual(hourly[1,0],0)
        self.assertTrue(negative[1,1])
        self.assertTrue(np.isnan(hourly[1,1:]).all())

    def test_duplicate_boundaries_require_identical_values(self):
        times,data = merge_segments([(self.times[1:3],np.array([[1.],[2.]])),
                                     (self.times[2:4],np.array([[2.],[3.]]))])
        self.assertEqual(len(times),3)
        np.testing.assert_array_equal(data[:,0],[1,2,3])
        with self.assertRaisesRegex(ValueError,'Conflicting duplicate'):
            merge_segments([(self.times[1:2],np.array([[1.]])),(self.times[1:2],np.array([[2.]]))])
        with self.assertRaisesRegex(ValueError,'unique'):
            deaccumulate_precipitation(np.array([[1.],[2.]]),self.times[[1,1]])

    def test_instantaneous_snow_analysis_is_valid_but_precipitation_requires_forecast_accumulation(self):
        time=SimpleNamespace(units='seconds since 1970-01-01',calendar='proleptic_gregorian')
        field=SimpleNamespace(units='m of water equivalent',GRIB_paramId=141,GRIB_dataType='an',
                              GRIB_stepType='instant',dimensions=('valid_time','latitude','longitude'))
        validate_metadata(field,time,'sd')
        field.units='m';field.GRIB_paramId=228;field.GRIB_stepType='accum'
        with self.assertRaisesRegex(ValueError,'Unexpected source metadata'):
            validate_metadata(field,time,'tp')
        field.GRIB_dataType='fc'
        validate_metadata(field,time,'tp')

    def test_catalog_observed_segments_include_year_boundary(self):
        path='files/d633008/e5land.oper.fc.sfc.accumu/202512/e5land.oper.fc.sfc.accumu.tp_228.2025122601-2026010100.nc'
        xml=f'<catalog><dataset urlPath="{path}"/><dataset urlPath="unrelated.tp_228.2025122601-2026010100.nc"/></catalog>'
        end=self.start+timedelta(days=3)
        self.assertEqual(catalog_segments(xml,'tp',self.start,end),[(path,self.start,end)])
        windows=list(request_windows([(path,self.start,end)],hours=24))
        self.assertEqual(len(windows),3)
        self.assertEqual(windows[0][1],self.start)
        self.assertEqual(windows[-1][2],end)
        self.assertTrue(all((last-first).total_seconds() == 86400 for _,first,last in windows))
        self.assertTrue(all(windows[i][2] == windows[i+1][1] for i in range(2)))
        self.assertEqual(len(list(request_windows([(path,self.start,end)]))),12)

    def test_failed_cache_replay_invalidates_old_completion(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);geo=root/'catchments.geojson'
            geo.write_text(json.dumps({'type':'FeatureCollection','features':[{'type':'Feature',
                'properties':{'id':'A','part':'local'},'geometry':{'type':'Polygon',
                'coordinates':[[[-120,45],[-119,45],[-119,46],[-120,46],[-120,45]]]}}]}))
            (root/'summary.json').write_text('{"status":"bounded_hourly_demo_complete"}')
            (root/'hourly.csv').write_text('stale output')
            with patch('sys.argv',['download','--geojson',str(geo),'--output',str(root),'--cache-only']),patch('download_era5_land.subprocess.run') as network:
                with self.assertRaisesRegex(ValueError,'Missing or invalid cached'):
                    main()
                network.assert_not_called()
            self.assertEqual(json.loads((root/'summary.json').read_text())['status'],'running')
            self.assertFalse((root/'hourly.csv').exists())


if __name__ == '__main__':
    unittest.main()
