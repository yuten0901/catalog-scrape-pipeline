"""Command line entry point.

Three commands, because a client needs exactly three things: check that a
configuration is sane, run it, and have something to run it against.

    catalog-scrape validate --config config/demo-local.yaml
    catalog-scrape scrape   --config config/demo-local.yaml
    catalog-scrape serve-demo --port 8800

Exit codes are part of the interface, since this is the sort of thing that ends
up in cron or a CI job:

====  ====================================================================
``0`` the run completed and every page was fetched
``1`` the run completed, but something needs a human: a page failed, or no
      products were exported. **The data that was collected is still written.**
``2`` the run never started -- the configuration is invalid
====  ====================================================================

The distinction between 1 and 2 is the useful one. ``2`` means "you typed
something wrong, nothing happened"; ``1`` means "the output is real but
incomplete, go read the report".
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from catalog_scraper import __version__
from catalog_scraper.clock import FixedClock, SystemClock
from catalog_scraper.config import PipelineConfig, load_config
from catalog_scraper.errors import ConfigError
from catalog_scraper.logging_setup import configure_logging
from catalog_scraper.models import RunReport
from catalog_scraper.pipeline import Pipeline

EXIT_OK = 0
EXIT_INCOMPLETE = 1
EXIT_CONFIG_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="catalog-scrape",
        description="Collect catalogue data from configured sources and export clean CSV/JSON.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scrape = subparsers.add_parser("scrape", help="run the pipeline")
    scrape.add_argument("--config", required=True, help="path to the YAML configuration")
    scrape.add_argument(
        "--source",
        action="append",
        default=None,
        metavar="ID",
        help="run only this source (repeatable). Useful when one source is broken.",
    )
    scrape.add_argument("--output-dir", default=None, help="override run.output_dir")
    scrape.add_argument(
        "--only-changed",
        action="store_true",
        help="export only new/changed products. Requires run.state_file.",
    )
    scrape.add_argument("--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    scrape.add_argument("--log-format", default=None, choices=["text", "json"])
    scrape.add_argument(
        "--now",
        default=None,
        metavar="ISO8601",
        help=(
            "pin the clock, e.g. 2026-08-24T09:00:00Z. Relative dates on pages "
            "('3 days ago') resolve against it, which is what makes the committed "
            "example output reproducible."
        ),
    )

    validate = subparsers.add_parser(
        "validate", help="load and check a configuration without fetching anything"
    )
    validate.add_argument("--config", required=True)

    demo = subparsers.add_parser("serve-demo", help="serve the bundled demo website")
    demo.add_argument("--host", default="127.0.0.1")
    demo.add_argument("--port", type=int, default=8800)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "serve-demo":
        from demo_site.server import main as serve

        sys.argv = ["serve-demo", "--host", args.host, "--port", str(args.port)]
        serve()
        return EXIT_OK

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        # Straight to stderr, not through the logger: the logger is configured
        # *from* the file that just failed to load.
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if args.command == "validate":
        return _validate(config)
    return _scrape(config, args)


def _validate(config: PipelineConfig) -> int:
    print(f"{config.source_path}: OK  (sha256 {config.source_sha256[:12]})")
    print(f"  output      {config.run.output_dir}  formats={','.join(config.run.formats)}")
    print(f"  state file  {config.run.state_file or '(none: change detection is off)'}")
    print(f"  dedupe key  {' + '.join(config.dedupe.key_fields)}  ({config.dedupe.on_conflict})")
    print(f"  http        timeout={config.http.timeout_seconds}s "
          f"attempts={config.http.max_attempts} delay={config.http.delay_seconds}s "
          f"robots={'on' if config.http.respect_robots else 'OFF'}")
    for source in config.sources:
        print(
            f"  source {source.id:<20} {source.kind:<8} "
            f"{source.pagination.strategy:<10} max_pages={source.pagination.max_pages}"
        )
        print(f"    {source.start_url}")
        print(f"    records: {source.record_selector}")
        for name, mapping in source.fields.items():
            suffix = f" @{mapping.attr}" if mapping.attr else ""
            print(f"      {name:<14} {mapping.selector}{suffix}")
    return EXIT_OK


def _scrape(config: PipelineConfig, args: argparse.Namespace) -> int:
    if args.output_dir:
        run = config.run.model_copy(update={"output_dir": args.output_dir})
        config = config.model_copy(update={"run": run})
    if args.source:
        selected = _select_sources(config, args.source)
        if selected is None:
            return EXIT_CONFIG_ERROR
        config = config.model_copy(update={"sources": selected})
    if args.only_changed and not config.run.state_file:
        print(
            "--only-changed needs run.state_file to be set: without it every product "
            "reports change=unknown and the filter would export nothing.",
            file=sys.stderr,
        )
        return EXIT_CONFIG_ERROR

    log = configure_logging(
        level=args.log_level or config.run.log_level,
        log_format=args.log_format or config.run.log_format,
    )

    clock = FixedClock(_parse_instant(args.now)) if args.now else SystemClock()
    pipeline = Pipeline(config, clock=clock, log=log, only_changed=args.only_changed)
    report = pipeline.run()
    _print_summary(report)
    return report.exit_code


def _select_sources(config: PipelineConfig, wanted: list[str]) -> list[object] | None:
    known = {source.id for source in config.sources}
    unknown = [name for name in wanted if name not in known]
    if unknown:
        print(
            f"unknown source(s): {', '.join(unknown)}. "
            f"This config defines: {', '.join(sorted(known))}",
            file=sys.stderr,
        )
        return None
    return [source for source in config.sources if source.id in set(wanted)]


def _parse_instant(text: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SystemExit(f"--now: {text!r} is not an ISO 8601 timestamp ({exc})") from exc
    if parsed.tzinfo is None:
        raise SystemExit("--now must include a timezone, e.g. 2026-08-24T09:00:00Z")
    return parsed


def _print_summary(report: RunReport) -> None:
    """The human-facing summary, on stdout.

    It is a rendering of ``run-report.json`` and nothing else -- no number is
    computed here -- so the console and the file can never disagree. That
    sounds pedantic until the two are computed separately and a client quotes
    the one that is wrong.
    """
    totals = report.totals
    print()
    print("=" * 72)
    print(f"  run {report.run_id}   {report.duration_seconds}s   exit {report.exit_code}")
    print("=" * 72)
    print(f"  pages fetched     {totals['pages_fetched']:>6}   failed {totals['pages_failed']}")
    print(f"  records found     {totals['records_found']:>6}")
    print(f"    exported        {report.exported:>6}")
    print(f"    rejected        {totals['rejected']:>6}")
    print(
        "    duplicates      "
        f"{totals['duplicates_identical'] + totals['duplicates_conflicting']:>6}"
        f"   ({totals['duplicates_conflicting']} conflicting)"
    )

    if report.changes:
        rendered = "  ".join(
            f"{name}={count}" for name, count in sorted(report.changes.items()) if count
        )
        print(f"  changes           {rendered or '(none)'}")

    if report.rejections_by_reason:
        print("\n  rejections by reason")
        for reason, count in report.rejections_by_reason.items():
            print(f"    {reason:<34} {count:>4}")

    if report.failures:
        print("\n  failed pages")
        for failure in report.failures:
            print(
                f"    [{failure.kind.value}] {failure.source_id} "
                f"p{failure.page_no} {failure.url}"
            )
            print(f"      {failure.message}")

    if report.warnings:
        print(f"\n  warnings ({len(report.warnings)})")
        for warning in report.warnings[:10]:
            print(f"    {warning}")
        if len(report.warnings) > 10:
            print(f"    ... and {len(report.warnings) - 10} more (see run-report.json)")

    print("\n  output")
    for name, path in sorted(report.outputs.items()):
        size = Path(path).stat().st_size if Path(path).exists() else 0
        print(f"    {name:<20} {path}  ({size:,} bytes)")
    print()


if __name__ == "__main__":
    raise SystemExit(main())
