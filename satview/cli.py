"""satview command line: run, list-sources, clean."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import warnings
from pathlib import Path
from typing import Any

import yaml

from satview import cache, config, pipeline, render, sources
from satview.areas import AREAS, RESOLUTION_M, get_area
from satview.config import SOURCE_NAMES, Config, ConfigError
from satview.sources.base import AreaDef, TimeSpec

log = logging.getLogger("satview")

NOISY_LOGGERS = ("satpy", "pyresample", "pyspectral", "pyorbital", "eumdac", "earthaccess", "urllib3",
                 "fsspec", "h5py", "trollimage", "PIL", "rasterio", "imageio", "asyncio")


def setup_logging(verbose: int) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG if verbose > 1 else logging.ERROR)
    if verbose < 2:
        warnings.filterwarnings("ignore", category=RuntimeWarning)
        warnings.filterwarnings("ignore", category=UserWarning)


def overrides(args: argparse.Namespace) -> dict[str, Any]:
    """Turn CLI flags into dotted config keys, applied on top of config.yaml."""
    result: dict[str, Any] = {}
    for item in args.set:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise ConfigError(f"--set expects KEY=VALUE, got {item!r}")
        result[key.strip()] = yaml.safe_load(value)
    at, start, end = getattr(args, "at", None), getattr(args, "start", None), getattr(args, "end", None)
    if at and (start or end):
        raise ConfigError("use either --at or --start/--end, not both")
    if at:
        result["time.mode"] = "at"
        result["time.at"] = at
    if start or end:
        if not (start and end):
            raise ConfigError("--start and --end must be given together")
        result.update({"time.mode": "range", "time.range.start": start, "time.range.end": end})
    if getattr(args, "step", None):
        result["time.range.step_minutes"] = args.step
    if getattr(args, "format", None):
        result["output.formats"] = args.format
    if getattr(args, "area", None):
        result["area"] = args.area
    if getattr(args, "output_dir", None):
        result["output.dir"] = str(args.output_dir.resolve())
    return result


def describe(when: TimeSpec) -> str:
    if when.mode == "latest":
        return "the latest scene"
    if when.mode == "at":
        return f"{when.at:%Y-%m-%d %H:%M}Z"
    return f"{when.start:%Y-%m-%d %H:%M}Z to {when.end:%Y-%m-%d %H:%M}Z every {when.step}"


def source_area(cfg: Config, name: str) -> AreaDef:
    """The area's shared grid, or the finer one a high-resolution source asks for (same extent)."""
    return get_area(getattr(cfg.source(name), "resolution_m", RESOLUTION_M), cfg.area)


def load_overlays(cfg: Config, area: AreaDef) -> render.OverlayData | None:
    if not cfg.output.overlays.any:
        return None
    try:
        return render.load_overlays(cfg.output.cache_dir / "naturalearth", area, cfg.output.overlays)
    except Exception as e:
        log.warning("overlays unavailable, rendering without them: %s", e)
        return None


def run_source(name: str, cfg: Config, when: TimeSpec, area: AreaDef,
               overlays: render.OverlayData | None, force: bool) -> bool:
    """Find, process and animate one source's scenes. True unless every scene failed."""
    try:
        source = sources.build(name, cfg)
        granules = source.find(when, area)
    except Exception as e:
        log.error("%s: search failed: %s: %s", name, type(e).__name__, e)
        log.debug("details", exc_info=True)
        return False
    if not granules:
        log.warning("%s: no data found for %s", name, describe(when))
        return False
    log.info("%s: %d scene(s) for %s", name, len(granules), describe(when))

    results = []
    for granule in granules:
        try:
            results.append(pipeline.process(source, granule, cfg, area, overlays, force))
        except Exception as e:
            log.error("%s %s: failed: %s: %s", name, granule.label, type(e).__name__, e)
            log.debug("details", exc_info=True)
            results.append(pipeline.Result(granule, "failed"))

    if when.mode == "range" and cfg.output.animation_formats:
        try:
            pipeline.animate(name, results, cfg.output, force)
        except Exception as e:
            log.error("%s: animation failed: %s: %s", name, type(e).__name__, e)
            log.debug("details", exc_info=True)
    return any(r.status != "failed" for r in results)


def cmd_run(args: argparse.Namespace) -> int:
    cfg = config.load(args.config, overrides(args), sources=args.source)
    if args.animate and not cfg.output.animation_formats:
        cfg.output.formats.append("gif")
    names = list(dict.fromkeys(args.source)) if args.source else cfg.enabled_sources()
    if not names:
        raise ConfigError("no sources enabled; enable one in the config or pass --source")
    cfg.check_credentials(names)

    when = cfg.time.spec()
    overlays = load_overlays(cfg, get_area(name=cfg.area))  # lon/lat lines, valid on every grid of the area
    ok = {name: run_source(name, cfg, when, source_area(cfg, name), overlays, args.force) for name in names}
    failed = [name for name, success in ok.items() if not success]
    if failed:
        log.warning("failed: %s", ", ".join(failed))
    # Non-zero only when nothing worked; one source down should not fail a cron job.
    return 1 if len(failed) == len(names) else 0


def cmd_list_sources(args: argparse.Namespace) -> int:
    cfg = config.load(args.config, overrides(args))
    healthy = True
    for name in SOURCE_NAMES:
        source_cfg = cfg.source(name)
        env_vars = source_cfg.env_vars()
        missing = source_cfg.missing_env_vars()
        if not env_vars:
            credentials = "none needed"
        elif missing:
            credentials = f"missing {', '.join(missing)}"
        else:
            credentials = f"set ({', '.join(v for v in env_vars if os.environ.get(v))})"
        print(f"{name:16} {'enabled' if source_cfg.enabled else 'disabled':9} credentials: {credentials}")
        if not source_cfg.enabled:
            continue
        if missing:
            healthy = False
        elif not args.offline:
            try:
                print(f"{'':26} check: OK, {sources.build(name, cfg).check()}")
            except Exception as e:
                healthy = False
                print(f"{'':26} check: FAILED, {type(e).__name__}: {e}")
    return 0 if healthy else 1


def cmd_clean(args: argparse.Namespace) -> int:
    cfg = config.load(args.config, overrides(args))
    keep = cfg.output.keep_raw_days
    files, size = cache.clean(cfg.output.cache_dir, keep, dry_run=args.dry_run)
    verb = "would remove" if args.dry_run else "removed"
    print(f"{verb} {files} file(s), {size / 1e6:.1f} MB older than {keep:g} day(s) from {cfg.output.cache_dir}")
    return 0


def _add_common(parser: argparse.ArgumentParser, suppress: bool = False) -> None:
    """Options accepted before or after the subcommand. The subcommand copies use SUPPRESS
    defaults so they do not overwrite values given before it."""

    def default(value):
        return argparse.SUPPRESS if suppress else value

    parser.add_argument("-c", "--config", type=Path, default=default(Path("config.yaml")),
                        help="config file (default: ./config.yaml)")
    parser.add_argument("--set", action="append", default=default([]), metavar="KEY=VALUE",
                        help="override any config key, e.g. --set output.width_px=800 (repeatable)")
    parser.add_argument("-v", "--verbose", action="count", default=default(0),
                        help="-v for debug output, -vv to include library logs and warnings")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    _add_common(common, suppress=True)

    parser = argparse.ArgumentParser(prog="satview", description="Satellite pictures of Israel showing clouds.")
    _add_common(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", parents=[common], help="download and render scenes (latest by default)")
    run.add_argument("--source", action="append", choices=SOURCE_NAMES,
                     help="run only this source, even if disabled in the config (repeatable)")
    run.add_argument("--area", choices=tuple(AREAS), help="area to draw (default from config)")
    run.add_argument("--at", metavar="TIME", help="scene nearest this UTC time, e.g. 2026-10-30T10:00Z")
    run.add_argument("--start", metavar="TIME", help="start of a time range (with --end)")
    run.add_argument("--end", metavar="TIME", help="end of a time range (with --start)")
    run.add_argument("--step", type=int, metavar="MINUTES", help="time range step (default from config)")
    run.add_argument("--animate", action="store_true", help="build a GIF from a time range")
    run.add_argument("--format", action="append", choices=("png", "geotiff", "gif", "mp4"),
                     help="output format, replaces the config list (repeatable)")
    run.add_argument("--output-dir", type=Path, help="output folder")
    run.add_argument("--force", action="store_true", help="re-render scenes whose files already exist")
    run.set_defaults(func=cmd_run)

    ls = sub.add_parser("list-sources", parents=[common], help="show sources and check credentials")
    ls.add_argument("--offline", action="store_true", help="only check that credentials are set")
    ls.set_defaults(func=cmd_list_sources)

    clean = sub.add_parser("clean", parents=[common], help="delete cached raw data older than keep_raw_days")
    clean.add_argument("--dry-run", action="store_true", help="only report what would be deleted")
    clean.set_defaults(func=cmd_clean)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        return args.func(args)
    except ConfigError as e:
        log.error("%s", e)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
