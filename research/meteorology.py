"""Explicit diagnostic meteorology; humidity/phase/wind are native-cell fields.

Saturation is over liquid water, including below freezing. Wet bulb is a
pressure-aware ventilated-psychrometer approximation, not an ice-bulb or
microphysical precipitation model. PET uses daily catchment-mean temperature
statistics and is an uncalibrated Hargreaves-Samani reference-ET estimate.
"""
import math

import numpy as np


DERIVED_FIELDS = {
    'relative_humidity_pct': ('%', 'instantaneous'),
    'vapor_pressure_deficit_kpa': ('kPa', 'instantaneous'),
    'wet_bulb_temperature_c': ('degC', 'instantaneous'),
    'rainfall_mm': ('mm', 'hour_ending_amount'),
    'snowfall_mm': ('mm', 'hour_ending_amount'),
    'wind_speed_ms': ('m/s', 'instantaneous'),
}
DERIVED_METHODS = {
    'spatial_order': 'All hourly diagnostics at native cell/hour before polygon-area averaging.',
    'saturation': {'method': 'FAO56 Tetens over liquid water, including subzero temperatures',
        'formula_kpa': '0.6108 * exp(17.27 * T_c / (T_c + 237.3))',
        'source': 'https://www.fao.org/4/X0490E/x0490e07.htm'},
    'humidity': {'vapor_pressure_kpa': '(p_pa / 1000) * q / (0.622 + 0.378 * q)',
        'relative_humidity_pct': '100 * e / es(T); supersaturation retained',
        'vapor_pressure_deficit_kpa': 'max(es(T) - e, 0)',
        'source': 'https://www.weather.gov/media/owp/operations/aorc_v1_1_methods.pdf'},
    'wet_bulb': {'method': 'Ferrel ventilated-psychrometer root, 32 bisections',
        'formula': 'es(Tw) - 0.00066*(1+0.00115*Tw)*p_kpa*(T-Tw) = min(e,es(T))',
        'convention': 'Liquid-water saturation; supersaturated inputs give Tw=T.',
        'source': 'https://repository.library.noaa.gov/view/noaa/1388/noaa_1388_DS1.pdf'},
    'phase': {'method': 'Binary wet-bulb partition of precipitation water equivalent',
        'snow_threshold_c': 0.5, 'snow_rule': 'Tw <= 0.5; otherwise rain',
        'timing': 'Instantaneous T/q/p at the precipitation hour-ending timestamp',
        'missing': 'Union of missing precipitation/T/q/p masks both phase amounts; no zero fill.',
        'closure': 'rainfall_mm + snowfall_mm = precipitation_mm on jointly valid cells'},
    'wind': {'formula': 'hypot(u_10m, v_10m) before spatial averaging'},
    'pet': {'method': 'FAO56 Hargreaves-Samani, uncalibrated catchment approximation',
        'formula_mm_day': 'max(0, 0.0023*(Tmean+17.8)*sqrt(Tmax-Tmin)*(0.408*Ra_MJ_m2_day))',
        'temperature_statistics': 'Mean/min/max of 24 hourly catchment means; not cellwise extremes.',
        'latitude': 'WGS84 lon/lat polygon centroid latitude',
        'radiation': 'FAO56 extraterrestrial radiation equations 21,23,24,25; Gsc=0.0820 MJ/m2/min; 365-day angular denominator',
        'sources': ['https://www.fao.org/4/X0490E/x0490e07.htm',
                    'https://www.fao.org/4/X0490E/x0490e08.htm']},
}


def saturation_vapor_pressure_kpa(temperature_c):
    """FAO56 equation 11, with the liquid-water convention at all temperatures."""
    temperature_c = np.asarray(temperature_c, dtype=float)
    return .6108 * np.exp(17.27 * temperature_c / (temperature_c + 237.3))


def _vapor(temperature_c, specific_humidity, pressure_pa):
    t, q, p = np.broadcast_arrays(np.asarray(temperature_c, dtype=float),
                                  np.asarray(specific_humidity, dtype=float),
                                  np.asarray(pressure_pa, dtype=float))
    valid = (np.isfinite(t) & np.isfinite(q) & np.isfinite(p)
             & (t >= -100) & (t <= 70) & (q >= 0) & (q < 1) & (p >= 10000) & (p <= 120000))
    t, q, p = (np.where(valid, value, np.nan) for value in (t, q, p))
    vapor = (p / 1000) * q / (.622 + .378 * q)
    return t, vapor, p / 1000


def humidity(temperature_c, specific_humidity, pressure_pa):
    """Return RH (%) and nonnegative VPD (kPa), retaining RH above 100%."""
    t, vapor, _ = _vapor(temperature_c, specific_humidity, pressure_pa)
    saturation = saturation_vapor_pressure_kpa(t)
    return 100 * vapor / saturation, np.maximum(saturation - vapor, 0.)


def wet_bulb_temperature(temperature_c, specific_humidity, pressure_pa):
    """Solve Ferrel's liquid-water psychrometer equation; missing inputs stay NaN."""
    t, vapor, pressure = _vapor(temperature_c, specific_humidity, pressure_pa)
    saturation = saturation_vapor_pressure_kpa(t)
    vapor = np.minimum(vapor, saturation)
    lower, upper = np.full(t.shape, -120.), t.copy()
    for _ in range(32):
        mid = (lower + upper) / 2
        residual = (saturation_vapor_pressure_kpa(mid)
                    - .00066 * (1 + .00115 * mid) * pressure * (t - mid) - vapor)
        lower, upper = np.where(residual < 0, mid, lower), np.where(residual < 0, upper, mid)
    return np.where(vapor == saturation, t, (lower + upper) / 2)


def derive_native(fields):
    """Derive synchronized native-cell arrays; each field uses its input-mask union."""
    inputs = [fields[key] for key in ('temperature_c', 'specific_humidity_kgkg', 'surface_pressure_pa')]
    rh, vpd = humidity(*inputs)
    wet_bulb = wet_bulb_temperature(*inputs)
    precipitation = np.asarray(fields['precipitation_mm'])
    valid = np.isfinite(wet_bulb) & np.isfinite(precipitation) & (precipitation >= 0)
    snow = np.where(valid, np.where(wet_bulb <= .5, precipitation, 0.), np.nan)
    rain = np.where(valid, precipitation - snow, np.nan)
    return dict(relative_humidity_pct=rh, vapor_pressure_deficit_kpa=vpd,
                wet_bulb_temperature_c=wet_bulb, rainfall_mm=rain, snowfall_mm=snow,
                wind_speed_ms=np.hypot(fields['u_wind_ms'], fields['v_wind_ms']))


def hargreaves_pet(tmean, tmin, tmax, latitude, day):
    """Daily mm/day estimate using FAO56 Ra converted from MJ/m2/day by 0.408."""
    if not all(math.isfinite(value) for value in (tmean, tmin, tmax, latitude)):
        raise ValueError('PET requires finite temperature statistics and centroid latitude')
    if not -90 <= latitude <= 90 or not tmin <= tmean <= tmax:
        raise ValueError('PET requires valid latitude and ordered temperature statistics')
    julian = day.timetuple().tm_yday
    phase = 2 * math.pi * julian / 365
    distance = 1 + .033 * math.cos(phase)
    declination = .409 * math.sin(phase - 1.39)
    latitude = math.radians(latitude)
    sunset = math.acos(max(-1., min(1., -math.tan(latitude) * math.tan(declination))))
    radiation = max(0., 24 * 60 / math.pi * .0820 * distance * (
        sunset * math.sin(latitude) * math.sin(declination)
        + math.cos(latitude) * math.cos(declination) * math.sin(sunset)))
    return max(0., .0023 * (tmean + 17.8) * math.sqrt(tmax - tmin) * (.408 * radiation))
