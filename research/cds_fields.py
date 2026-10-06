"""CDS field definitions and grid-cell calculations before spatial aggregation."""

import numpy as np


# CDS names, NetCDF units and temporal processing are explicit to prevent aliases
# such as snow depth water equivalent from being interpreted as physical depth.
LAND_STATES = {
    't2m': ('2m_temperature', 'K'),
    'sd': ('snow_depth_water_equivalent', 'm of water equivalent'),
    'sde': ('snow_depth', 'm'),
    'rsn': ('snow_density', 'kg m**-3'),
    'snowc': ('snow_cover', '%'),
    **{f'swvl{i}': (f'volumetric_soil_water_layer_{i}', 'm**3 m**-3') for i in range(1, 5)},
    **{f'stl{i}': (f'soil_temperature_level_{i}', 'K') for i in range(1, 5)},
}
LAND_ACCUMULATED = {
    'tp': ('total_precipitation', 'm'),
    'smlt': ('snowmelt', 'm of water equivalent'),
    'e': ('total_evaporation', 'm of water equivalent'),
    'ssr': ('surface_net_solar_radiation', 'J m**-2'),
    'str': ('surface_net_thermal_radiation', 'J m**-2'),
}
ERA_SURFACE = {'tcc': ('total_cloud_cover', '1'),
               'sp': ('surface_pressure', 'Pa'), 'z': ('geopotential', 'm**2 s**-2')}
ERA_PROFILE = {'t': ('temperature', 'K'), 'z': ('geopotential', 'm**2 s**-2')}
PRESSURE_LEVELS = [1000, 975, 950, 925, 900, 875, 850, 825, 800, 775, 750,
                   700, 650, 600, 550, 500, 450, 400, 350, 300]
ALL_FIELDS = {**LAND_STATES, **LAND_ACCUMULATED, **ERA_SURFACE, **ERA_PROFILE}
ALIASES = {'2t': 't2m'}
GRAVITY = 9.80665


def root_zone_moisture(arrays):
    """Thickness-weighted volumetric water content over the configured 0–100 cm."""
    return .07*arrays['swvl1'] + .21*arrays['swvl2'] + .72*arrays['swvl3']


def land_daily_fields(states, accumulated):
    """Return (array, units, statistic, flags); states have 24 hourly slices."""
    result = {}
    for name, statistic in [('tmean_c', 'mean'), ('tmin_c', 'minimum'), ('tmax_c', 'maximum')]:
        result[name] = (states['t2m']-273.15, 'degC', statistic, None)
    for key, name, units, factor in [
        ('sd', 'snow_water_equivalent_mm', 'mm', 1000),
        ('sde', 'snow_depth_m', 'm', 1),
        ('rsn', 'snow_density_kgm3', 'kg/m3', 1),
        ('snowc', 'snow_cover_pct', '%', 1),
    ]:
        result[name] = (states[key]*factor, units, 'mean', None)
    for i in range(1, 5):
        result[f'soil_moisture_layer{i}_m3m3'] = (states[f'swvl{i}'], 'm3/m3', 'mean', None)
        result[f'soil_temperature_layer{i}_c'] = (states[f'stl{i}']-273.15, 'degC', 'mean', None)
    result['root_zone_soil_moisture_0_100cm_m3m3'] = (root_zone_moisture(states), 'm3/m3', 'mean', None)
    for key, name, units, factor in [
        ('tp', 'precipitation_mm', 'mm', 1000),
        ('smlt', 'snowmelt_mm', 'mm', 1000),
        ('e', 'actual_evapotranspiration_mm', 'mm', -1000),
        ('ssr', 'net_shortwave_energy_mjm2', 'MJ/m2', 1e-6),
        ('str', 'net_longwave_energy_mjm2', 'MJ/m2', 1e-6),
    ]:
        data = accumulated[key]*factor
        if key in {'tp', 'smlt'}:
            data = np.where(data >= -1e-4, np.maximum(data, 0), np.nan)
        result[name] = (data, units, '24h_endpoint', None)
    net = accumulated['ssr'] + accumulated['str']
    result['net_radiation_energy_mjm2'] = (net*1e-6, 'MJ/m2', '24h_endpoint', None)
    result['net_radiation_mean_wm2'] = (net/86400, 'W/m2', '24h_endpoint', None)
    return result


def freezing_level(temperature_k, geopotential, pressure_hpa, surface_pressure_pa,
                   surface_geopotential):
    """Single bracketed warm-to-cold 0°C crossing, in geopotential metres.

    Profiles are (level, y, x), with pressure ordered from high to low. Below-
    ground levels are excluded by pressure and height. Missing intermediate
    levels, multiple crossings and unbracketed crossings never get extrapolated.
    """
    t, z = np.asarray(temperature_k, float), np.asarray(geopotential, float)/GRAVITY
    p = np.asarray(pressure_hpa, float)
    terrain = np.asarray(surface_geopotential, float)/GRAVITY
    sp = np.asarray(surface_pressure_pa, float)
    if (t.shape != z.shape or t.ndim != 3 or len(p) != len(t)
            or t.shape[1:] != sp.shape or sp.shape != terrain.shape
            or not np.all(np.diff(p) < 0)):
        raise ValueError('Invalid profile dimensions or pressure ordering')
    pressure_above = p[:, None, None]*100 <= sp
    above = pressure_above & (z >= terrain)
    adjacent = above[:-1] & above[1:]
    warm = t >= 273.15
    falling = adjacent & warm[:-1] & ~warm[1:]
    rising = adjacent & ~warm[:-1] & warm[1:]
    crossing_count = (falling | rising).sum(axis=0)
    flags = np.full(sp.shape, 'no_crossing', dtype=object)
    flags[crossing_count > 1] = 'multiple_crossings'
    valid = (falling.sum(axis=0) == 1) & (crossing_count == 1)
    flags[valid] = 'valid'
    with np.errstate(divide='ignore', invalid='ignore'):
        crossing_height = z[:-1] + (t[:-1]-273.15)/(t[:-1]-t[1:])*(z[1:]-z[:-1])
    zero_start = z.copy()
    for level in range(1, len(t)):
        plateau = adjacent[level-1] & (t[level-1] == 273.15) & (t[level] == 273.15)
        zero_start[level] = np.where(plateau, zero_start[level-1], zero_start[level])
    crossing_height = np.where(t[:-1] == 273.15, zero_start[:-1], crossing_height)
    result = np.where(valid, np.where(falling, crossing_height, 0).sum(axis=0), np.nan)
    starts = above & ~np.concatenate([np.zeros_like(above[:1]), above[:-1]])
    invalid = (starts.sum(axis=0) > 1) | np.any(adjacent & (np.diff(z, axis=0) <= 0), axis=0)
    missing = np.any((pressure_above & ~np.isfinite(z)) | (above & ~np.isfinite(t)), axis=0)
    flags[invalid] = 'invalid_profile'
    flags[missing] = 'missing_profile'
    flags[~np.isfinite(sp) | ~np.isfinite(terrain)] = 'missing_surface'
    result[flags != 'valid'] = np.nan
    return result, result-terrain, flags


def era_daily_fields(surface, profiles, levels):
    heights, above_ground, flags = [], [], []
    for hour in range(24):
        h, agl, qc = freezing_level(profiles['t'][hour], profiles['z'][hour], levels,
                                    surface['sp'][hour], surface['z'][hour])
        heights.append(h); above_ground.append(agl); flags.append(qc)
    flags = np.array(flags)
    return {
        'cloud_cover_fraction': (surface['tcc'], '1', 'mean', None),
        'freezing_level_geopotential_height_m': (np.array(heights), 'm', 'mean', flags),
        'freezing_level_above_terrain_m': (np.array(above_ground), 'm', 'mean', flags),
    }


def daily_schema(product):
    """The schema is generated from the same definitions used for actual output."""
    if product == 'era5-land':
        states = {key: np.zeros((24, 1, 1)) for key in LAND_STATES}
        accumulated = {key: np.zeros((1, 1)) for key in LAND_ACCUMULATED}
        fields = land_daily_fields(states, accumulated)
    else:
        fields = {'cloud_cover_fraction': (None, '1', 'mean', None),
                  'freezing_level_geopotential_height_m': (None, 'm', 'mean', None),
                  'freezing_level_above_terrain_m': (None, 'm', 'mean', None)}
    return {key: {'units': units, 'statistic': statistic} for key, (_, units, statistic, _) in fields.items()}
