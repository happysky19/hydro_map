"""Command-line entry points."""

import argparse
from dataclasses import replace
import json
import math
from pathlib import Path
import sys

from .config import load_config


def _parser():
    parser = argparse.ArgumentParser(description="Build and compare HydroBASINS catchments.")
    commands = parser.add_subparsers(dest="command", required=True)
    download = commands.add_parser("download", help="Download a regional HydroBASINS dataset")
    download.add_argument("--config", type=Path, required=True)
    download.add_argument("--cache-dir", type=Path, default=Path("data"))
    build = commands.add_parser("build", help="Aggregate units for configured projects")
    build.add_argument("config", type=Path)
    build.add_argument("--cache-dir", type=Path, default=Path("data"))
    build.add_argument("--source", type=Path, help="Use an existing regional .shp file")
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--csv-output", type=Path, help="Also write a project bounding-box CSV")
    build.add_argument("--table-output", type=Path, help="Also write project attributes and WKT geometry in one CSV")
    build.add_argument("--bbox-buffer", type=float, default=0.3, help="CSV bounding-box padding in degrees (default: 0.3)")
    build.add_argument("--part", choices=("local", "total"), default="local")
    build.add_argument("--projects", nargs="+", help="Filter outputs; all YAML projects remain upstream cutoffs")
    virtual = build.add_mutually_exclusive_group()
    virtual.add_argument("--include-virtual", dest="virtual", action="store_true")
    virtual.add_argument("--exclude-virtual", dest="virtual", action="store_false")
    build.set_defaults(virtual=None)
    plot = commands.add_parser("plot", help="Compare GeoJSON or shapefile boundaries")
    plot.add_argument("inputs", type=Path, nargs="+")
    selection = plot.add_mutually_exclusive_group()
    selection.add_argument("--project", help="Match a project ID or name")
    selection.add_argument("--by-project", action="store_true", help="Write one figure per project and an overview")
    plot.add_argument("--labels", nargs="+", help="One legend label per input")
    plot.add_argument("--id-field", default="id")
    plot.add_argument("--name-field", default="name")
    plot.add_argument("--title")
    plot.add_argument("--coordinates", choices=("lonlat", "projected"), default="lonlat")
    plot.add_argument("--basemap", choices=("none", "terrain", "light"), default="none")
    plot.add_argument("--tile-cache", type=Path, default=Path("data/map_tiles"))
    plot.add_argument("--output", type=Path)
    plot.add_argument("--output-dir", type=Path)
    return parser


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command in {"download", "build"}:
            config = load_config(args.config)
            if args.command == "build" and (not math.isfinite(args.bbox_buffer) or args.bbox_buffer < 0):
                raise ValueError("Bounding-box buffer must be finite and nonnegative")
            if args.command == "build" and args.virtual is not None:
                config = replace(config, dataset=replace(config.dataset, include_virtual_connections=args.virtual))
            if args.command == "build" and args.source:
                source = args.source
            else:
                from .data import download_dataset
                dataset = config.dataset
                source = download_dataset(dataset.region, dataset.level, dataset.version, args.cache_dir)
            if args.command == "download":
                print(source)
                return 0
            from .basins import build_catchments
            inputs = {source.with_suffix(suffix).resolve() for suffix in (".shp", ".shx", ".dbf", ".prj")}
            inputs.add(args.config.resolve())
            if args.output.resolve() in inputs:
                raise ValueError("Output must differ from the input files")
            if args.csv_output and args.csv_output.resolve() in inputs | {args.output.resolve()}:
                raise ValueError("CSV output must differ from the input files and GeoJSON output")
            if args.table_output and args.table_output.resolve() in inputs | {args.output.resolve()} | (
                    {args.csv_output.resolve()} if args.csv_output else set()):
                raise ValueError("Geometry table output must differ from inputs and other outputs")
            result = build_catchments(config, source, args.part, args.projects)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
            print(f"Wrote {len(result['features'])} {args.part} catchments to {args.output}")
            shared = result["metadata"]["shared_outlet_groups"]
            if shared:
                count = sum(len(members) for members in shared.values())
                print(f"Shared-unit approximations: {count} projects in {len(shared)} groups; count each forcing_group once for area or volume.")
            if args.csv_output:
                from .csv_export import write_bbox_csv
                count = write_bbox_csv(result, args.csv_output, args.bbox_buffer)
                print(f"Wrote {count} project summaries to {args.csv_output}")
            if args.table_output:
                from .csv_export import write_geometry_csv
                count = write_geometry_csv(result, args.table_output)
                print(f"Wrote {count} project rows with geometry to {args.table_output}")
            repairs = result["metadata"]["repaired_geometry_count"]
            if repairs:
                print(f"Repaired {repairs} source geometries; unit IDs are recorded in the output metadata.")
            return 0

        from .plotting import plot_by_project, plot_comparison
        options = {"labels": args.labels, "id_field": args.id_field, "name_field": args.name_field,
                   "coordinates": args.coordinates, "basemap": args.basemap, "tile_cache": args.tile_cache}
        if args.by_project:
            if args.output or not args.output_dir:
                raise ValueError("--by-project requires --output-dir and cannot use --output")
            paths = plot_by_project(args.inputs, args.output_dir, **options)
            overview = args.output_dir / "overview.png"
            used = {path.name.casefold() for path in args.output_dir.iterdir()}
            index = 2
            while overview.name.casefold() in used:
                overview = args.output_dir / f"overview-{index}.png"
                index += 1
            plot_comparison(args.inputs, overview, title=args.title, **options)
            print(f"Wrote {len(paths)} project figures and {overview}")
        else:
            if not args.output or args.output_dir:
                raise ValueError("plot requires --output; use --by-project for --output-dir")
            report = plot_comparison(args.inputs, args.output, project=args.project, title=args.title, **options)
            print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, OSError) as error:
        print(f"hydro-map: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
