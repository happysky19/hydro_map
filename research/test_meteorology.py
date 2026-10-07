"""Check native-cell meteorology, phase closure and the daily PET energy units."""
import datetime as dt
import unittest

import numpy as np

from meteorology import (derive_native, hargreaves_pet, humidity,
                        saturation_vapor_pressure_kpa, wet_bulb_temperature)


def specific_humidity(vapor_kpa, pressure_pa):
    return .622 * vapor_kpa / (pressure_pa / 1000 - .378 * vapor_kpa)


class MeteorologyTests(unittest.TestCase):
    def test_fao_saturation_and_known_relative_humidity(self):
        self.assertAlmostEqual(saturation_vapor_pressure_kpa(24.5), 3.075, places=3)
        q = specific_humidity(1.2, 90000.)
        rh, vpd = humidity(20., q, 90000.)
        self.assertAlmostEqual(float(rh), 51.31974561, places=6)
        self.assertAlmostEqual(float(vpd), 1.138281271, places=8)

    def test_wet_bulb_recovers_pressure_dependent_psychrometer_reading(self):
        # Construct actual vapor pressure for a 10C wet bulb / 20C dry bulb.
        for pressure in (60000., 101325.):
            vapor = saturation_vapor_pressure_kpa(10.) - .00066 * (1 + .00115 * 10) * (pressure / 1000) * 10
            q = specific_humidity(vapor, pressure)
            self.assertAlmostEqual(float(wet_bulb_temperature(20., q, pressure)), 10., places=6)
        q = specific_humidity(1.2, 101325.)
        self.assertLess(float(wet_bulb_temperature(20., q, 60000.)),
                        float(wet_bulb_temperature(20., q, 101325.)))

    def test_newton_wet_bulb_matches_fine_bisection(self):
        rng = np.random.default_rng(7)
        t = rng.uniform(-45, 45, 20000)
        pressure = rng.uniform(55000, 105000, 20000)
        vapor = saturation_vapor_pressure_kpa(t) * rng.uniform(.02, 1., 20000)
        q = specific_humidity(vapor, pressure)
        lower, upper = np.full(t.shape, -120.), t.copy()
        for _ in range(60):
            mid = (lower + upper) / 2
            residual = (saturation_vapor_pressure_kpa(mid)
                        - .00066 * (1 + .00115 * mid) * (pressure / 1000) * (t - mid) - vapor)
            lower, upper = np.where(residual < 0, mid, lower), np.where(residual < 0, upper, mid)
        np.testing.assert_allclose(wet_bulb_temperature(t, q, pressure), (lower + upper) / 2, rtol=0, atol=1e-9)

    def test_saturation_supersaturation_and_missing_union(self):
        q = specific_humidity(saturation_vapor_pressure_kpa(5.) * 1.1, 90000.)
        rh, vpd = humidity(5., q, 90000.)
        self.assertAlmostEqual(float(rh), 110.)
        self.assertEqual(float(vpd), 0.)
        self.assertAlmostEqual(float(wet_bulb_temperature(5., q, 90000.)), 5., places=6)
        rh, vpd = humidity([5., np.nan, 5., 5.], [q, q, np.nan, q], [90000., 90000., 90000., np.nan])
        self.assertTrue(np.isnan(rh[1:]).all() and np.isnan(vpd[1:]).all())

    def test_phase_is_cellwise_and_missing_humidity_masks_only_its_products(self):
        temperatures = np.array([[-2., 3., 0.5, 3.]])
        pressure = np.full((1, 4), 90000.)
        q = specific_humidity(saturation_vapor_pressure_kpa(temperatures), pressure)
        q[0, 3] = np.nan
        result = derive_native(dict(temperature_c=temperatures, specific_humidity_kgkg=q,
            surface_pressure_pa=pressure, precipitation_mm=np.array([[2., 4., 6., 8.]]),
            u_wind_ms=np.array([[3., -3., np.nan, 3.]]), v_wind_ms=np.array([[4., -4., 4., 4.]])))
        np.testing.assert_allclose(result['snowfall_mm'][0, :3], [2., 0., 6.])
        np.testing.assert_allclose(result['rainfall_mm'][0, :3], [0., 4., 0.])
        np.testing.assert_allclose((result['snowfall_mm'] + result['rainfall_mm'])[0, :3], [2., 4., 6.])
        self.assertTrue(np.isnan(result['rainfall_mm'][0, 3]))
        np.testing.assert_allclose(result['wind_speed_ms'][0, [0, 1, 3]], [5., 5., 5.])
        self.assertTrue(np.isnan(result['wind_speed_ms'][0, 2]))
        # Opposing wind components average to zero but their native speeds do not.
        self.assertEqual(result['wind_speed_ms'][0, :2].mean(), 5.)

    def test_hargreaves_includes_radiation_conversion_and_cold_day_floor(self):
        # FAO56 example 8: Sep3,20S, Ra=32.2MJ/m2/day (rounded reference).
        expected = .0023 * (20 + 17.8) * np.sqrt(10.) * .408 * 32.2
        self.assertAlmostEqual(hargreaves_pet(20., 15., 25., -20., dt.date(2025, 9, 3)), expected, delta=.01)
        self.assertEqual(hargreaves_pet(-25., -30., -20., 45., dt.date(2024, 2, 29)), 0.)
        self.assertEqual(hargreaves_pet(10., 10., 10., 45., dt.date(2025, 6, 21)), 0.)
        self.assertEqual(hargreaves_pet(10., 5., 15., 90., dt.date(2025, 12, 21)), 0.)
        with self.assertRaises(ValueError):
            hargreaves_pet(20., 25., 15., 45., dt.date(2025, 6, 21))


if __name__ == '__main__':
    unittest.main()
