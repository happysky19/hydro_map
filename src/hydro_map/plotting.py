"""Load polygon datasets and render comparisons with freshly computed areas."""

import json
import math
import re
import textwrap
from pathlib import Path

import shapefile
from pyproj import CRS, Geod, Transformer
from shapely.geometry import shape
from shapely.geometry.polygon import orient
from shapely.ops import transform, unary_union

_GEOD = Geod(ellps='WGS84')


def load_features(path: Path, id_field='id', name_field='name') -> list[dict]:
    """Read polygon features in WGS84; shapefiles require a CRS in .prj."""
    path = Path(path)
    if path.suffix.lower() == '.shp':
        prj = path.with_suffix('.prj')
        if not prj.exists():
            raise ValueError(f'Shapefile requires a .prj file: {path}')
        crs = CRS.from_wkt(prj.read_text())
        convert = Transformer.from_crs(crs, 4326, always_xy=True).transform
        with shapefile.Reader(str(path)) as reader:
            raw = [{'type': 'Feature', 'properties': row.record.as_dict(),
                    'geometry': row.shape.__geo_interface__}
                   for row in reader.iterShapeRecords()]
    elif path.suffix.lower() in {'.json', '.geojson'}:
        doc = json.loads(path.read_text())
        if doc.get('crs'):
            raise ValueError('GeoJSON must use WGS84 coordinates without a legacy crs member')
        if doc.get('type') == 'FeatureCollection':
            raw = doc['features']
        elif doc.get('type') == 'Feature':
            raw = [doc]
        else:
            raw = [{'type': 'Feature', 'properties': {}, 'geometry': doc}]
        convert = None
    else:
        raise ValueError(f'Unsupported input format: {path.suffix}')
    if not raw:
        raise ValueError(f'No polygon features in {path}')
    result = []
    for feature in raw:
        geometry = shape(feature['geometry'])
        if geometry.geom_type not in {'Polygon', 'MultiPolygon'}:
            raise ValueError(f'Only Polygon and MultiPolygon geometry is supported: {path}')
        if convert:
            geometry = transform(convert, geometry)
        if geometry.is_empty or not geometry.is_valid:
            raise ValueError(f'Empty or invalid polygon in {path}')
        west, south, east, north = geometry.bounds
        if not (-180 <= west <= east <= 180 and -90 <= south <= north <= 90):
            raise ValueError(f'Coordinates outside WGS84 bounds: {path}')
        properties = dict(feature.get('properties') or {})
        identifier = properties.get(id_field)
        if identifier is None or str(identifier).strip() == '':
            identifier = feature.get('id')
        name = properties.get(name_field)
        if identifier is None or str(identifier).strip() == '':
            identifier = name if name is not None else path.stem
        if name is None or str(name).strip() == '':
            name = str(identifier)
        result.append({**feature, 'geometry': geometry, 'properties': properties,
                       'id': str(identifier), 'name': str(name)})
    return result


def _select(features, project, path):
    if project is None:
        return features
    key = str(project).strip().casefold()
    selected = [f for f in features if key in {f['id'].strip().casefold(),
                                              f['name'].strip().casefold()}]
    if not selected:
        raise ValueError(f'Project {project!r} is missing from {path}')
    identifiers = {f['id'].casefold() for f in selected}
    if len(identifiers) > 1:
        raise ValueError(f'Project {project!r} matches multiple identifiers in {path}')
    return selected


def _polygon_patch(polygon, color, linestyle='-', alpha=.14):
    """Create a compound path with opposite winding for interior holes."""
    from matplotlib.path import Path as MplPath
    from matplotlib.patches import PathPatch

    polygon = orient(polygon, sign=1)
    vertices, codes = [], []
    for ring in [polygon.exterior, *polygon.interiors]:
        coords = list(ring.coords)
        vertices.extend(coords)
        codes.extend([MplPath.MOVETO] + [MplPath.LINETO] * (len(coords) - 2)
                     + [MplPath.CLOSEPOLY])
    return PathPatch(MplPath(vertices, codes), facecolor=color, edgecolor=color,
                     alpha=alpha, linewidth=1.2, linestyle=linestyle)


def _map_decorations(axes, crs):
    """Add a local geodesic scale and a north arrow in the displayed CRS."""
    import matplotlib.patheffects as effects

    inverse = Transformer.from_crs(crs, 4326, always_xy=True).transform
    forward = Transformer.from_crs(4326, crs, always_xy=True).transform
    west, east = axes.get_xlim()
    south, north = axes.get_ylim()
    width, height = east - west, north - south
    x, y = west + .07 * width, south + .075 * height
    lon, lat = inverse(x, y)
    far_lon, far_lat = inverse(x + .22 * width, y)
    target_km = abs(_GEOD.inv(lon, lat, far_lon, far_lat)[2]) / 1000
    magnitude = 10 ** math.floor(math.log10(target_km))
    length_km = max(value * magnitude for value in (1, 2, 5) if value * magnitude <= target_km)
    end_lon, end_lat, _ = _GEOD.fwd(lon, lat, 90, length_km * 1000)
    end_x, end_y = forward(end_lon, end_lat)
    axes.plot([x, end_x], [y, end_y], color='#203548', linewidth=2.2,
              marker='|', markersize=8, zorder=20,
              path_effects=[effects.withStroke(linewidth=5, foreground='white')])
    axes.annotate(f'{length_km:g} km', ((x + end_x) / 2, (y + end_y) / 2),
                  xytext=(0, 7), textcoords='offset points', ha='center', fontsize=9,
                  color='#203548', zorder=21,
                  bbox={'facecolor': 'white', 'edgecolor': 'none', 'alpha': .85, 'pad': 2})
    x, y = west + .07 * width, south + .82 * height
    lon, lat = inverse(x, y)
    next_lon, next_lat, _ = _GEOD.fwd(lon, lat, 0, 1000)
    nx, ny = forward(next_lon, next_lat)
    dx, dy = (nx - x) / width, (ny - y) / height
    norm = math.hypot(dx, dy)
    tip = (.07 + .07 * dx / norm, .82 + .07 * dy / norm)
    axes.annotate('', tip, xytext=(.07, .82), xycoords='axes fraction',
                  arrowprops={'facecolor': '#203548', 'edgecolor': 'white', 'width': 3, 'headwidth': 9},
                  zorder=20)
    axes.text(tip[0], tip[1] + .015, 'N', transform=axes.transAxes, ha='center',
              fontsize=10, weight='bold', color='#203548', zorder=21,
              path_effects=[effects.withStroke(linewidth=3, foreground='white')])


def plot_comparison(paths, output: Path, project: str | None = None,
                    labels: list[str] | None = None, id_field='id',
                    name_field='name', title: str | None = None,
                    coordinates='lonlat', basemap='none',
                    tile_cache: Path = Path('data/map_tiles')) -> dict:
    """Compare polygons with geographic or projected axes and optional map tiles."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.patches import Patch
    from matplotlib.ticker import FuncFormatter, MaxNLocator
    import matplotlib.patheffects as effects

    if coordinates not in {'lonlat', 'projected'}:
        raise ValueError('coordinates must be lonlat or projected')

    paths = [Path(p) for p in paths]
    if not paths:
        raise ValueError('At least one input path is required')
    labels = list(labels) if labels is not None else [p.stem for p in paths]
    if len(labels) != len(paths):
        raise ValueError('The label count must equal the input path count')
    if len(set(labels)) != len(labels):
        raise ValueError('Overlay labels must be unique')
    output = Path(output)
    if output.suffix.lower() not in {'.png', '.pdf', '.svg'}:
        raise ValueError('Output must have a .png, .pdf, or .svg extension')
    datasets = [_select(load_features(p, id_field, name_field), project, p) for p in paths]
    geometries = [unary_union([f['geometry'] for f in features]) for features in datasets]
    areas = {}
    for label, geometry in zip(labels, geometries):
        polygons = list(geometry.geoms) if geometry.geom_type == 'MultiPolygon' else [geometry]
        areas[label] = sum(abs(_GEOD.geometry_area_perimeter(orient(p, sign=1))[0])
                          for p in polygons) / 1e6
    centre = unary_union(geometries).centroid
    map_crs = (CRS.from_epsg(4326) if coordinates == 'lonlat' else
               CRS.from_proj4(f'+proj=laea +lat_0={centre.y} +lon_0={centre.x} +datum=WGS84 +units=m'))
    project_xy = Transformer.from_crs(4326, map_crs, always_xy=True).transform

    selected_name = datasets[0][0]['name'] if project is not None or len(datasets[0]) == 1 else 'All projects'
    heading = title or selected_name
    figure = Figure(figsize=(10.5, 8.4), facecolor='white')
    FigureCanvasAgg(figure)
    axes = figure.subplots()
    figure.subplots_adjust(left=.10, right=.97, bottom=.10, top=.865 - .026 * len(labels))
    axes.set_facecolor('#f1f5f5')
    colors = ['#126782', '#ce5724', '#28816c', '#84519b', '#a57816', '#5186b5']
    styles = ['-', '--', '-.', ':']
    handles = []
    marker_positions = set()
    for i, (features, geometry, label) in enumerate(zip(datasets, geometries, labels)):
        color, style = colors[i % len(colors)], styles[i % len(styles)]
        for feature in features:
            projected = transform(project_xy, feature['geometry'])
            polygons = projected.geoms if projected.geom_type == 'MultiPolygon' else [projected]
            for polygon in polygons:
                patch = _polygon_patch(polygon, color, style, alpha=.09 if basemap != 'none' else .14)
                patch.set_zorder(3)
                axes.add_patch(patch)
                for ring in [polygon.exterior, *polygon.interiors]:
                    x, y = ring.xy
                    axes.plot(x, y, color=color, linestyle=style, linewidth=1.6, zorder=4,
                              path_effects=[effects.withStroke(linewidth=2.7, foreground='white', alpha=.75)])
        handles.append(Patch(facecolor=color, edgecolor=color, alpha=.5,
                             linestyle=style, label=label))
        for feature in features:
            props = {str(k).lower(): v for k, v in feature['properties'].items()}
            lat = next((props[k] for k in ('dam_lat', 'latitude', 'lat') if k in props), None)
            lon = next((props[k] for k in ('dam_lon', 'longitude', 'lon') if k in props), None)
            if lat is None or lon is None:
                continue
            lat, lon = float(lat), float(lon)
            if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError(f'Invalid dam coordinates for {feature["id"]}')
            if (lon, lat) not in marker_positions:
                axes.plot(*project_xy(lon, lat), marker='^', color='#203548', markeredgecolor='white',
                          markersize=8, markeredgewidth=1, linestyle='none', zorder=10)
                if project is not None or len(features) == 1:
                    axes.annotate(feature['name'], project_xy(lon, lat), xytext=(9, 7),
                                  textcoords='offset points', fontsize=9, color='#203548',
                                  zorder=11, path_effects=[effects.withStroke(linewidth=3, foreground='white')])
                marker_positions.add((lon, lat))
    if marker_positions:
        from matplotlib.lines import Line2D
        handles.append(Line2D([], [], marker='^', color='#203548', linestyle='none', label='Dam reference'))
    axes.autoscale_view()
    axes.margins(.12)
    aspect = 1 / max(.05, math.cos(math.radians(centre.y))) if coordinates == 'lonlat' else 1
    axes.set_aspect(aspect, adjustable='box', anchor='W')
    west, east = axes.get_xlim()
    south, north = axes.get_ylim()
    map_ratio = (east - west) / ((north - south) * aspect)
    figure_width = max(6.6, min(13, 8.4 * (.765 - .026 * len(labels)) * map_ratio / .87))
    figure.set_size_inches(figure_width, 8.4)
    axes.set_xlabel('Longitude' if coordinates == 'lonlat' else 'Easting (km)', color='#435566', labelpad=8)
    axes.set_ylabel('Latitude' if coordinates == 'lonlat' else 'Northing (km)', color='#435566', labelpad=8)
    if coordinates == 'lonlat':
        axes.xaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{abs(x):g}°{'W' if x < 0 else 'E'}"))
        axes.yaxis.set_major_formatter(FuncFormatter(lambda y, _: f"{abs(y):g}°{'S' if y < 0 else 'N'}"))
    else:
        axes.xaxis.set_major_formatter(FuncFormatter(lambda x, _: f'{x / 1000:g}'))
        axes.yaxis.set_major_formatter(FuncFormatter(lambda y, _: f'{y / 1000:g}'))
    axes.xaxis.set_major_locator(MaxNLocator(5))
    axes.yaxis.set_major_locator(MaxNLocator(5))
    attribution = ''
    if basemap != 'none':
        from .basemaps import add_basemap
        attribution = add_basemap(axes, map_crs, basemap, tile_cache)
    axes.grid(color='#65798b', alpha=.18, linewidth=.6, linestyle=':')
    axes.tick_params(colors='#566778', labelsize=9, length=3)
    for spine in axes.spines.values():
        spine.set_color('#aebdc5')
        spine.set_linewidth(.7)
    _map_decorations(axes, map_crs)
    figure.text(.10, .952, heading, ha='left', va='top', fontsize=23, weight='bold', color='#203548')
    for index, (label, value) in enumerate(areas.items()):
        figure.text(.10, .9 - index * .026, f'{label}  ·  {value:,.1f} km²',
                    fontsize=11, color=colors[index % len(colors)])
    legend = axes.legend(handles=handles, loc='upper right', fontsize=9, framealpha=.96,
                         facecolor='white', edgecolor='#d5dfe3', borderpad=.8)
    legend.set_zorder(30)
    if attribution:
        axes.text(.99, .01, textwrap.fill(attribution, width=int(figure_width * .85 * 22)),
                  transform=axes.transAxes, ha='right', va='bottom', fontsize=6,
                  color='#566778', zorder=30,
                  bbox={'facecolor': 'white', 'edgecolor': 'none', 'alpha': .8, 'pad': 2})
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=220, bbox_inches='tight', pad_inches=.18)
    return {'project': selected_name, 'areas_km2': areas, 'output': str(output),
            'coordinate_display': coordinates, 'basemap': basemap}


def plot_by_project(paths, output_dir: Path, labels: list[str] | None = None,
                    id_field='id', name_field='name', title: str | None = None,
                    coordinates='lonlat', basemap='none',
                    tile_cache: Path = Path('data/map_tiles')) -> list[Path]:
    """Plot every first-file identifier, requiring the same IDs in all inputs.

    Existing outputs and sanitized filename collisions are rejected before saving.
    """
    paths = [Path(p) for p in paths]
    if not paths:
        raise ValueError('At least one input path is required')
    datasets = [load_features(p, id_field, name_field) for p in paths]
    identifiers = list(dict.fromkeys(f['id'] for f in datasets[0]))
    expected = {key.casefold() for key in identifiers}
    for path, features in zip(paths[1:], datasets[1:]):
        if {f['id'].casefold() for f in features} != expected:
            raise ValueError(f'Project identifiers do not match across inputs: {path}')
    output_dir = Path(output_dir)
    outputs = [output_dir / (re.sub(r'[^A-Za-z0-9_-]+', '_', key).strip('_') or 'project')
               for key in identifiers]
    outputs = [p.with_name(p.name + '.png') for p in outputs]
    if len({p.name.casefold() for p in outputs}) != len(outputs):
        raise ValueError('Project identifiers cause output filename collisions')
    for output in outputs:
        if output.exists():
            raise FileExistsError(f'Output already exists: {output}')
    for key, output in zip(identifiers, outputs):
        plot_comparison(paths, output, project=key, labels=labels, id_field=id_field,
                        name_field=name_field, title=title, coordinates=coordinates,
                        basemap=basemap, tile_cache=tile_cache)
    return outputs
