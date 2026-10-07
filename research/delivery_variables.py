"""Requested delivery columns: provenance, method, units and forecast-model counterparts.

REQUESTED follows the requested variable table (category, variable) in order.
Each entry selects one column of the full export without changing its values;
soil temperature keeps the four ERA5-Land layers. Forecast names are parameter
short names in ECMWF IFS open data (0.25 deg), NOAA GFS 0.25 deg GRIB2
(VARIABLE:level) and WeatherNext 2 output fields.
"""

AORC, LAND, ERA5 = 'aorc_v1.1', 'era5_land_cds', 'era5_cds'
SOURCE_LABELS = {AORC: 'NOAA AORC v1.1 (hourly, 30 arc-second)',
                 LAND: 'ECMWF ERA5-Land (hourly, 0.1 deg)',
                 ERA5: 'ECMWF ERA5 single levels (hourly, 0.25 deg)'}
NOT_AVAILABLE = 'not available'
DERIVE = 'derive from T, humidity and pressure as here'
SEASONAL = ('Mean of 24 hourly catchment means over seasonal-snow cells; ERA5-Land cells holding '
            '>= 5 m water equivalent all day (glaciers) are excluded, see perennial_snow_area_pct')


def entry(category, quantity, source, variable, origin, definition, ifs, gfs, wn2, note=''):
    return dict(category=category, quantity=quantity, source=source, variable=variable, origin=origin,
                definition=definition, ifs=ifs, gfs=gfs, wn2=wn2, note=note)


REQUESTED = [
    entry('Precipitation', 'Total Precipitation', AORC, 'precipitation_mm', 'native APCP_surface',
          'Sum of the 24 hour-ending amounts from 01 to 24 UTC',
          'tp (accumulated since initial time; difference steps)', 'APCP:surface',
          'total_precipitation_6hr (sum of four steps)'),
    entry('Precipitation', 'Rainfall', AORC, 'rainfall_mm', 'computed from precipitation, T, q, p',
          'Hourly precipitation where wet bulb > 0.5 degC, per cell, summed over the day',
          'tp split with the same wet-bulb rule', 'APCP split with the same wet-bulb rule',
          NOT_AVAILABLE + ' (no near-surface humidity)',
          'IFS sf and GFS CPOFP/CSNOW are model phase fields with different definitions.'),
    entry('Precipitation', 'Snowfall', AORC, 'snowfall_mm', 'computed from precipitation, T, q, p',
          'Hourly precipitation where wet bulb <= 0.5 degC, per cell, summed over the day (water equivalent)',
          'tp split with the same wet-bulb rule', 'APCP split with the same wet-bulb rule',
          NOT_AVAILABLE + ' (no near-surface humidity)', 'Rainfall + snowfall = total precipitation.'),
    entry('Temperature', '2m Air Temperature', AORC, 'tmean_c', 'native TMP_2maboveground',
          'Mean of the 24 hourly (00-23 UTC) catchment means', '2t', 'TMP:2 m above ground',
          '2m_temperature (6-hourly)', 'With 6-hourly output the daily mean uses four samples.'),
    entry('Temperature', 'Daily Max Temperature', AORC, 'tmax_c', 'computed from TMP_2maboveground',
          'Maximum of the 24 hourly catchment means',
          'mx2t3/mx2t6 or maximum of hourly 2t', 'TMAX:2 m above ground (6-hour maximum)',
          'maximum of 6-hourly 2m_temperature',
          'Grid-point period extremes averaged over the basin are warmer than this; '
          'take the extreme of the basin-mean forecast series instead.'),
    entry('Temperature', 'Daily Min Temperature', AORC, 'tmin_c', 'computed from TMP_2maboveground',
          'Minimum of the 24 hourly catchment means',
          'mn2t3/mn2t6 or minimum of hourly 2t', 'TMIN:2 m above ground (6-hour minimum)',
          'minimum of 6-hourly 2m_temperature',
          'Grid-point period extremes averaged over the basin are colder than this; '
          'take the extreme of the basin-mean forecast series instead.'),
    entry('Snow', 'Snow Water Equivalent (SWE)', LAND, 'snow_water_equivalent_mm', 'native sd',
          SEASONAL, 'sd', 'WEASD:surface (kg/m2 = mm)', NOT_AVAILABLE,
          'Apply the same glacier exclusion to forecast fields.'),
    entry('Snow', 'Snow Depth', LAND, 'snow_depth_m', 'native sde',
          SEASONAL, 'sd x 1000 / rsn', 'SNOD:surface', NOT_AVAILABLE),
    entry('Snow', 'Snowmelt', LAND, 'snowmelt_mm', 'native smlt',
          '24-hour accumulation to 24 UTC (water equivalent; includes glacier cells)',
          'smlt (full IFS archive; not in open data)', NOT_AVAILABLE, NOT_AVAILABLE),
    entry('Freezing Level', 'Freezing Level Height', ERA5, 'freezing_level_above_ground_m', 'native deg0l',
          'ECMWF 0 degC level height above ground (0 when the column is below 0 degC); '
          'mean of 24 hourly catchment means',
          'deg0l (full IFS archive; not in open data)', 'HGT:0C isotherm - HGT:surface',
          'derive from 13 pressure-level t and z',
          'Definitions differ with several 0 degC crossings; compare climatologies first.'),
    entry('Freezing Level', '0°C Isotherm Elevation', ERA5, 'freezing_level_above_sea_level_m',
          'computed from deg0l + surface geopotential',
          'deg0l + z/9.80665 per cell-hour (equals terrain height when the column is below 0 degC), '
          'then averaged; metres above sea level',
          'deg0l + orography', 'HGT:0C isotherm', 'derive from 13 pressure-level t and z'),
    entry('Soil', 'Surface Soil Moisture', LAND, 'soil_moisture_layer1_m3m3', 'native swvl1',
          'Volumetric water content of the 0-7 cm layer; mean of 24 hourly catchment means',
          'vsw level 1 (same layer)', 'SOILW:0-0.1 m below ground (different layer)', NOT_AVAILABLE),
    entry('Soil', 'Root Zone Soil Moisture', LAND, 'root_zone_soil_moisture_0_100cm_m3m3',
          'computed from swvl1-3', '0-100 cm thickness-weighted: 0.07 L1 + 0.21 L2 + 0.72 L3 per cell-hour, '
          'then averaged', 'vsw levels 1-3 with the same weights',
          'SOILW 0-0.1, 0.1-0.4, 0.4-1 m weighted 0.1/0.3/0.6', NOT_AVAILABLE),
    entry('Soil', 'Deep Soil Moisture', LAND, 'soil_moisture_layer4_m3m3', 'native swvl4',
          'Volumetric water content of the 100-289 cm layer; mean of 24 hourly catchment means',
          'vsw level 4 (same layer)', 'SOILW:1-2 m below ground (different layer)', NOT_AVAILABLE),
    *[entry('Soil', f'Soil Temperature ({depth})', LAND, f'soil_temperature_layer{i}_c', f'native stl{i}',
            f'Layer {i} ({depth}); mean of 24 hourly catchment means', f'sot level {i} (same layer)',
            f'TSOIL:{gfs} m below ground (different layer)', NOT_AVAILABLE)
      for i, depth, gfs in [(1, '0-7 cm', '0-0.1'), (2, '7-28 cm', '0.1-0.4'),
                            (3, '28-100 cm', '0.4-1'), (4, '100-289 cm', '1-2')]],
    entry('Atmosphere', 'Relative Humidity', AORC, 'relative_humidity_pct', 'computed from T, q, p',
          '2 m: 100 e/es(T), FAO-56 saturation over liquid water, per cell-hour then averaged',
          'from 2t, 2d', 'recompute from TMP, SPFH, PRES', NOT_AVAILABLE,
          'GFS RH:2 m uses its own saturation convention; recompute for consistency.'),
    entry('Wind', 'Wind Speed', AORC, 'wind_speed_ms', 'computed from 10 m U/V',
          '10 m: sqrt(u^2+v^2) in every cell and hour, then averaged',
          'from 10u/10v', 'from UGRD/VGRD:10 m', 'from 10 m u/v (6-hourly)',
          'Compute speed before averaging; the magnitude of mean u/v is smaller.'),
    entry('Wind', 'U-Wind Component', AORC, 'u_wind_ms', 'native UGRD_10maboveground',
          '10 m eastward component; mean of 24 hourly catchment means', '10u', 'UGRD:10 m above ground',
          '10m_u_component_of_wind'),
    entry('Wind', 'V-Wind Component', AORC, 'v_wind_ms', 'native VGRD_10maboveground',
          '10 m northward component; mean of 24 hourly catchment means', '10v', 'VGRD:10 m above ground',
          '10m_v_component_of_wind'),
    entry('Radiation', 'Shortwave Radiation', AORC, 'shortwave_down_mean_wm2', 'native DSWRF_surface',
          'Incoming (downward) shortwave at the surface; mean of 24 hourly catchment means',
          'ssrd (J/m2 accumulated; daily difference / 86400)', 'DSWRF:surface', NOT_AVAILABLE),
    entry('Radiation', 'Net Radiation', LAND, 'net_radiation_mean_wm2', 'computed from ssr + str',
          'Net shortwave plus net longwave over the 24-hour accumulation / 86400; positive downward',
          'ssr + str', '(DSWRF - USWRF) + (DLWRF - ULWRF)', NOT_AVAILABLE),
    entry('Water Balance', 'Potential Evapotranspiration (PET)', AORC, 'pet_hargreaves_mm',
          'computed from Tmean/Tmin/Tmax',
          'Hargreaves-Samani with FAO-56 extraterrestrial radiation at the catchment centroid',
          'from daily 2t statistics', 'from daily TMP statistics', 'from daily 2m_temperature statistics',
          'Uses the daily temperature statistics above; keep their definitions identical.'),
    entry('Water Balance', 'Actual Evapotranspiration (AET)', LAND, 'actual_evapotranspiration_mm',
          'native e (sign reversed)', '24-hour accumulation to 24 UTC; positive is water loss',
          'e (full IFS archive; not in open data)', 'LHTFL:surface x 86400 / 2.45e6', NOT_AVAILABLE),
    entry('Atmosphere', 'Vapor Pressure Deficit', AORC, 'vapor_pressure_deficit_kpa', 'computed from T, q, p',
          '2 m: max(es(T) - e, 0) per cell-hour, then averaged', 'from 2t, 2d', DERIVE, NOT_AVAILABLE),
    entry('Atmosphere', 'Cloud Cover', ERA5, 'cloud_cover_fraction', 'native tcc',
          'Total cloud cover; mean of 24 hourly catchment means, 0-1', 'tcc',
          'TCDC:entire atmosphere (% / 100)', NOT_AVAILABLE),
]
