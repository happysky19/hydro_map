"""CDS field definitions and grid-cell calculations before spatial aggregation."""

import threading

import numpy as np

from meteorology import humidity, saturation_vapor_pressure_kpa, wet_bulb_temperature


# netCDF4 and the HDF5 library beneath it are not thread-safe: every NetCDF read or write
# in the process holds this lock, while downloads and decoding stay parallel.
NETCDF_LOCK = threading.RLock()

# CDS names, NetCDF units and temporal processing are explicit to prevent aliases
# such as snow depth water equivalent from being interpreted as physical depth.
LAND_STATES = {
    't2m': ('2m_temperature', 'K'),
    'd2m': ('2m_dewpoint_temperature', 'K'),
    'sp': ('surface_pressure', 'Pa'),
    'u10': ('10m_u_component_of_wind', 'm s**-1'),
    'v10': ('10m_v_component_of_wind', 'm s**-1'),
    'sd': ('snow_depth_water_equivalent', 'm of water equivalent'),
    'sde': ('snow_depth', 'm'),
    'rsn': ('snow_density', 'kg m**-3'),
    'snowc': ('snow_cover', '%'),
    **{f'swvl{i}': (f'volumetric_soil_water_layer_{i}', 'm**3 m**-3') for i in range(1, 5)},
    **{f'stl{i}': (f'soil_temperature_level_{i}', 'K') for i in range(1, 5)},
}
LAND_ACCUMULATED = {
    'tp': ('total_precipitation', 'm'),
    'sf': ('snowfall', 'm of water equivalent'),
    'smlt': ('snowmelt', 'm of water equivalent'),
    'e': ('total_evaporation', 'm of water equivalent'),
    'ssrd': ('surface_solar_radiation_downwards', 'J m**-2'),
    'strd': ('surface_thermal_radiation_downwards', 'J m**-2'),
    'ssr': ('surface_net_solar_radiation', 'J m**-2'),
    'str': ('surface_net_thermal_radiation', 'J m**-2'),
}
# deg0l is ECMWF's model-level 0 degC height above ground (zero when the whole
# column is below freezing); z is the time-invariant surface geopotential.
ERA_SURFACE = {'tcc': ('total_cloud_cover', '1'),
               'deg0l': ('zero_degree_level', 'm'),
               'z': ('geopotential', 'm**2 s**-2')}
ALL_FIELDS = {**LAND_STATES, **LAND_ACCUMULATED, **ERA_SURFACE}
ALIASES = {'2t': 't2m', '2d': 'd2m', '10u': 'u10', '10v': 'v10'}
GRAVITY = 9.80665
# ERA5-Land keeps accumulating snow on glacier cells (6-10 m water equivalent in
# these catchments); seasonal snow never approaches this. Such cells are excluded
# from the snow-state means so that they describe the seasonal snowpack.
PERENNIAL_SNOW_M = 5.
# Snow-free cells carry a nominal 100 kg/m3 density; density is averaged over snow only.
SNOW_PRESENT_M = .001


def root_zone_moisture(arrays):
    """Thickness-weighted volumetric water content over the configured 0–100 cm."""
    return .07*arrays['swvl1'] + .21*arrays['swvl2'] + .72*arrays['swvl3']


def dewpoint_humidity(temperature_k, dewpoint_k, pressure_pa):
    """Specific humidity, RH and VPD from 2 m dewpoint with AORC's saturation formula."""
    vapor = saturation_vapor_pressure_kpa(np.asarray(dewpoint_k, float) - 273.15)
    pressure = np.asarray(pressure_pa, float) / 1000
    specific = .622 * vapor / (pressure - .378 * vapor)
    rh, vpd = humidity(np.asarray(temperature_k, float) - 273.15, specific, pressure_pa)
    return specific, rh, vpd


def _amount(data):
    """Water amounts in mm; tiny negative packing noise becomes zero, larger negatives missing."""
    return np.where(data >= -1e-4, np.maximum(data, 0), np.nan)


def land_daily_fields(states, accumulated):
    """Return (array, units, statistic, excluded cells); states have 24 hourly slices."""
    result = {}
    perennial = np.all(states['sd'] >= PERENNIAL_SNOW_M, axis=0)
    seasonal_only = np.broadcast_to(perennial, states['sd'].shape)
    for name, statistic in [('tmean_c', 'mean'), ('tmin_c', 'minimum'), ('tmax_c', 'maximum')]:
        result[name] = (states['t2m']-273.15, 'degC', statistic, None)
    specific, rh, vpd = dewpoint_humidity(states['t2m'], states['d2m'], states['sp'])
    result['specific_humidity_kgkg'] = (specific, 'kg/kg', 'mean', None)
    result['relative_humidity_pct'] = (rh, '%', 'mean', None)
    result['vapor_pressure_deficit_kpa'] = (vpd, 'kPa', 'mean', None)
    result['wet_bulb_temperature_c'] = (
        wet_bulb_temperature(states['t2m']-273.15, specific, states['sp']), 'degC', 'mean', None)
    result['surface_pressure_pa'] = (states['sp'], 'Pa', 'mean', None)
    result['u_wind_ms'] = (states['u10'], 'm/s', 'mean', None)
    result['v_wind_ms'] = (states['v10'], 'm/s', 'mean', None)
    result['wind_speed_ms'] = (np.hypot(states['u10'], states['v10']), 'm/s', 'mean', None)
    for key, name, units, factor in [
        ('sd', 'snow_water_equivalent_mm', 'mm', 1000),
        ('sde', 'snow_depth_m', 'm', 1),
        ('rsn', 'snow_density_kgm3', 'kg/m3', 1),
        ('snowc', 'snow_cover_pct', '%', 1),
    ]:
        excluded = seasonal_only | (states['sd'] < SNOW_PRESENT_M) if key == 'rsn' else seasonal_only
        result[name] = (states[key]*factor, units, 'mean', excluded)
    result['perennial_snow_area_pct'] = (np.broadcast_to(perennial*100., states['sd'].shape), '%', 'mean', None)
    for i in range(1, 5):
        result[f'soil_moisture_layer{i}_m3m3'] = (states[f'swvl{i}'], 'm3/m3', 'mean', None)
        result[f'soil_temperature_layer{i}_c'] = (states[f'stl{i}']-273.15, 'degC', 'mean', None)
    result['root_zone_soil_moisture_0_100cm_m3m3'] = (root_zone_moisture(states), 'm3/m3', 'mean', None)
    precipitation = _amount(accumulated['tp']*1000)
    snowfall = _amount(accumulated['sf']*1000)
    result['precipitation_mm'] = (precipitation, 'mm', '24h_endpoint', None)
    result['snowfall_mm'] = (np.minimum(snowfall, precipitation), 'mm', '24h_endpoint', None)
    result['rainfall_mm'] = (np.maximum(precipitation - snowfall, 0), 'mm', '24h_endpoint', None)
    result['snowmelt_mm'] = (_amount(accumulated['smlt']*1000), 'mm', '24h_endpoint', None)
    result['actual_evapotranspiration_mm'] = (accumulated['e']*-1000, 'mm', '24h_endpoint', None)
    for key, name in [('ssrd', 'shortwave_down'), ('strd', 'longwave_down'),
                      ('ssr', 'net_shortwave'), ('str', 'net_longwave')]:
        result[f'{name}_energy_mjm2'] = (accumulated[key]*1e-6, 'MJ/m2', '24h_endpoint', None)
        result[f'{name}_mean_wm2'] = (accumulated[key]/86400, 'W/m2', '24h_endpoint', None)
    net = accumulated['ssr'] + accumulated['str']
    result['net_radiation_energy_mjm2'] = (net*1e-6, 'MJ/m2', '24h_endpoint', None)
    result['net_radiation_mean_wm2'] = (net/86400, 'W/m2', '24h_endpoint', None)
    return result


def era_daily_fields(surface):
    """Cloud cover and the native ECMWF 0 degC level, above ground and above the geoid."""
    return {
        'cloud_cover_fraction': (surface['tcc'], '1', 'mean', None),
        'freezing_level_above_ground_m': (surface['deg0l'], 'm', 'mean', None),
        'freezing_level_above_sea_level_m': (surface['deg0l'] + surface['z']/GRAVITY, 'm', 'mean', None),
    }


def daily_schema(product):
    """The schema is generated from the same definitions used for actual output."""
    if product == 'era5-land':
        states = {key: np.full((24, 1, 1), 280.) for key in LAND_STATES}
        states['sp'] = np.full((24, 1, 1), 1e5)
        accumulated = {key: np.zeros((1, 1)) for key in LAND_ACCUMULATED}
        fields = land_daily_fields(states, accumulated)
    else:
        fields = era_daily_fields({key: np.zeros((24, 1, 1)) for key in ERA_SURFACE})
    return {key: {'units': units, 'statistic': statistic} for key, (_, units, statistic, _) in fields.items()}
