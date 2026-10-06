"""Optional cached map tiles, warped to the plot's coordinate system."""

from pathlib import Path

from . import __version__


def add_basemap(ax, crs, style='terrain', cache_dir=Path('data/map_tiles')) -> str:
    """Add muted tiles behind geometry and return provider attribution."""
    if style == 'none':
        return ''
    if style not in {'terrain', 'light'}:
        raise ValueError(f'Unknown basemap style: {style!r}; choose terrain, light, or none')
    try:
        import contextily as cx
    except ImportError as error:
        raise ValueError("Basemaps require the maps extra: pip install -e '.[maps]'") from error

    source = cx.providers.Esri.WorldTopoMap if style == 'terrain' else cx.providers.Esri.WorldGrayCanvas
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cx.set_cache_dir(str(cache_dir))
    try:
        cx.add_basemap(ax, crs=crs, source=source, reset_extent=True,
                       attribution=False, zorder=0, alpha=.85, timeout=30,
                       headers={'user-agent': f'hydro-map/{__version__}'})
    except OSError as error:
        raise OSError(f'Could not load {style} basemap: {error}. Retry or use --basemap none.') from error
    return source.attribution
