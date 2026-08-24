"""Orchestration: pages in, four datasets and a report out.

Everything interesting has already been decided by the time this module runs —
what a field is (:mod:`~catalog_scraper.fields`), how to read it
(:mod:`~catalog_scraper.normalize`), when to retry
(:mod:`~catalog_scraper.fetch.http`), which copy to keep
(:mod:`~catalog_scraper.dedupe`). What is left here is the loop, and one
principle it exists to enforce:

    **A page that failed must never be indistinguishable from a page that was
    empty, and neither may quietly shrink the output.**

Concretely: no ``except`` in this file swallows anything. A page-level failure
ends that *source's* pagination, is recorded as a
:class:`~catalog_scraper.models.PageFailure`, and makes the process exit
non-zero — while the records collected before it are still exported. Partial
results are worth delivering; partial results presented as complete are not.
"""

from __future__ import annotations

import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from catalog_scraper.clock import Clock, Sleeper, SystemClock, SystemSleeper
from catalog_scraper.config import PipelineConfig, SourceConfig
from catalog_scraper.dedupe import Deduplicator
from catalog_scraper.errors import FetchError
from catalog_scraper.export import ProductRow, write_outputs, write_report
from catalog_scraper.extract import build_product, extract_records, find_next_url
from catalog_scraper.fetch.base import FetchedPage, Fetcher
from catalog_scraper.fetch.browser import BrowserFetcher
from catalog_scraper.fetch.http import HttpFetcher
from catalog_scraper.logging_setup import RunLogger, null_logger
from catalog_scraper.models import (
    ChangeStatus,
    DuplicateKind,
    DuplicateRecord,
    FailureKind,
    PageFailure,
    Product,
    RawRecord,
    RejectedRecord,
    RunReport,
    SourceReport,
)
from catalog_scraper.state import ChangeTracker

MAX_REPORTED_WARNINGS = 50
"""Warnings are truncated in the report, and the truncation is itself reported.

A run against a redesigned site can emit thousands of identical warnings; a
report nobody can open has told the client nothing.
"""


class Pipeline:
    """One configured run. Construct, call :meth:`run`, read the report."""

    def __init__(
        self,
        config: PipelineConfig,
        *,
        clock: Clock | None = None,
        sleeper: Sleeper | None = None,
        log: RunLogger | None = None,
        http_fetcher: Fetcher | None = None,
        browser_fetcher: Fetcher | None = None,
        only_changed: bool = False,
    ) -> None:
        self._config = config
        self._clock = clock or SystemClock()
        self._sleeper = sleeper or SystemSleeper()
        self._log = log or null_logger()
        self._only_changed = only_changed

        # The HTTP fetcher is always built: it owns the robots.txt policy and the
        # per-host throttle, and the browser fetcher borrows both so that a run
        # using two transports still presents one identity and obeys one set of
        # rules.
        self._http: Fetcher = http_fetcher or HttpFetcher(
            config.http, clock=self._clock, sleeper=self._sleeper, log=self._log
        )
        self._browser_override = browser_fetcher
        self._browser: Fetcher | None = browser_fetcher

        self._products: list[Product] = []
        self._rejected: list[RejectedRecord] = []
        self._duplicates: list[DuplicateRecord] = []
        self._failures: list[PageFailure] = []
        self._warnings: list[str] = []
        self._warning_count = 0
        self._dedupe = Deduplicator(config.dedupe)

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    def run(self) -> RunReport:
        started = self._clock.now()
        monotonic_started = time.monotonic()
        run_id = uuid.uuid4().hex[:12]
        self._log.info(
            "run.started",
            run_id=run_id,
            config=self._config.source_path,
            config_sha256=self._config.source_sha256[:12],
            sources=len(self._config.sources),
        )

        source_reports: list[SourceReport] = []
        try:
            for source in self._config.sources:
                source_reports.append(self._run_source(source))
        finally:
            # Closed here rather than in a caller: a browser left running after
            # an exception is a process that outlives the run and is invisible
            # unless you know its name.
            self._close_fetchers()

        report = self._finish(
            run_id,
            started,
            source_reports,
            duration_seconds=time.monotonic() - monotonic_started,
        )
        self._log_summary(report)
        return report

    # ------------------------------------------------------------------
    # one source
    # ------------------------------------------------------------------

    def _run_source(self, source: SourceConfig) -> SourceReport:
        report = SourceReport(source_id=source.id, kind=source.kind)
        self._log.info("source.started", source=source.id, kind=source.kind, url=source.start_url)

        fetcher = self._fetcher_for(source)
        url: str | None = _first_url(source)
        page_no = source.pagination.start_page
        visited: set[str] = set()
        fetched = 0

        while url is not None and fetched < source.pagination.max_pages:
            if url in visited:
                # Sites do produce pagination cycles (a "next" link on the last
                # page pointing back to page 1). Without this the run would
                # crawl forever and the duplicate count would climb quietly.
                report.stopped_because = "loop_detected"
                self._log.warning("pagination.loop", source=source.id, url=url)
                break
            visited.add(url)

            try:
                page = fetcher.fetch(url, wait_for=source.wait_for_selector)
            except FetchError as exc:
                self._record_failure(source, url, page_no, exc, report)
                report.stopped_because = "page_failed"
                break

            report.pages_fetched += 1
            records = extract_records(
                page.html, source=source, page_url=page.url, page_no=page_no
            )
            self._log.info(
                "page.fetched",
                source=source.id,
                page=page_no,
                url=page.url,
                status=page.status,
                attempts=page.attempts,
                records=len(records),
                via=page.fetched_with,
                seconds=round(page.elapsed_seconds, 3),
            )

            if not records:
                stopped = self._handle_empty_page(source, page, page_no, fetched, report)
                report.stopped_because = stopped
                break

            report.records_found += len(records)
            for raw in records:
                self._process_record(raw, source, report)

            fetched += 1
            url, reason = self._next_url(source, page, page_no)
            page_no += 1
            if url is None:
                report.stopped_because = reason
        else:
            if url is not None:
                report.stopped_because = "max_pages"
                self._log.warning(
                    "pagination.capped",
                    source=source.id,
                    max_pages=source.pagination.max_pages,
                    note="the catalogue may be larger than the export",
                )

        self._log.info(
            "source.finished",
            source=source.id,
            pages=report.pages_fetched,
            found=report.records_found,
            valid=report.valid,
            rejected=report.rejected,
            duplicates=report.duplicates_identical + report.duplicates_conflicting,
            stopped=report.stopped_because,
        )
        return report

    def _handle_empty_page(
        self,
        source: SourceConfig,
        page: FetchedPage,
        page_no: int,
        fetched: int,
        report: SourceReport,
    ) -> str:
        """Decide whether "zero records" is the end of the data or a broken selector.

        On any page after the first it is the ordinary way a ``page_param``
        catalogue ends. On the **first** page it is not: the fetch succeeded, so
        the selector matched nothing, which almost always means the site was
        redesigned. That is a failure with a name, not a quiet empty CSV.
        """
        if fetched == 0:
            self._record_failure(
                source,
                page.url,
                page_no,
                FetchError(
                    FailureKind.NO_RECORDS,
                    f"HTTP {page.status} but record_selector "
                    f"{source.record_selector!r} matched nothing. The page loaded, so the "
                    f"selector is probably out of date.",
                ),
                report,
            )
            return "no_records"
        self._log.info("pagination.exhausted", source=source.id, page=page_no)
        return "empty_page"

    def _process_record(
        self, raw: RawRecord, source: SourceConfig, report: SourceReport
    ) -> None:
        outcome, warnings = build_product(raw, source=source, now=self._clock.now())
        self._add_warnings(warnings)

        if isinstance(outcome, RejectedRecord):
            report.rejected += 1
            self._rejected.append(outcome)
            self._log.debug(
                "record.rejected",
                source=source.id,
                page=raw.page_no,
                reasons=",".join(outcome.reason_codes),
            )
            return

        duplicate = self._dedupe.add(outcome)
        if duplicate is None:
            report.valid += 1
            self._products.append(outcome)
            return

        self._duplicates.append(duplicate)
        if duplicate.kind is DuplicateKind.IDENTICAL:
            report.duplicates_identical += 1
        else:
            report.duplicates_conflicting += 1
            self._log.warning(
                "record.duplicate_conflict",
                source=source.id,
                key=duplicate.key.replace("\x1f", "|"),
                differs_on=",".join(duplicate.differing_fields),
                kept=duplicate.kept_url,
                dropped=duplicate.dropped_url,
            )

    # ------------------------------------------------------------------
    # pagination
    # ------------------------------------------------------------------

    def _next_url(
        self, source: SourceConfig, page: FetchedPage, page_no: int
    ) -> tuple[str | None, str]:
        strategy = source.pagination.strategy
        if strategy == "none":
            return None, "single_page"
        if strategy == "next_link":
            assert source.pagination.next_selector is not None  # enforced by config
            following = find_next_url(
                page.html, selector=source.pagination.next_selector, page_url=page.url
            )
            return following, "no_next_link" if following is None else ""
        assert source.pagination.page_param is not None  # enforced by config
        return _with_page_param(source.start_url, source.pagination.page_param, page_no + 1), ""

    # ------------------------------------------------------------------
    # finishing
    # ------------------------------------------------------------------

    def _finish(
        self,
        run_id: str,
        started: datetime,
        source_reports: list[SourceReport],
        *,
        duration_seconds: float,
    ) -> RunReport:
        finished = self._clock.now()
        config = self._config
        output_dir = Path(config.run.output_dir)

        tracker = (
            ChangeTracker.load(config.run.state_file)
            if config.run.state_file
            else ChangeTracker.disabled()
        )

        survivors = self._dedupe.products()
        rows = [
            ProductRow(
                product=product,
                key=self._dedupe.key_for(product),
                change=tracker.classify(
                    self._dedupe.key_for(product), product.content_hash
                ),
            )
            for product in survivors
        ]

        change_counts = Counter(row.change.value for row in rows)
        exported_rows = rows
        filtered_out = 0
        if self._only_changed:
            exported_rows = [
                row for row in rows if row.change in {ChangeStatus.NEW, ChangeStatus.CHANGED}
            ]
            filtered_out = len(rows) - len(exported_rows)

        outputs = write_outputs(
            output_dir=output_dir,
            formats=config.run.formats,
            rows=exported_rows,
            rejected=self._rejected,
            duplicates=self._duplicates,
            failures=self._failures,
            csv_encoding=config.run.csv_encoding,
        )

        # The state file is written from *every* surviving product, not only the
        # exported ones: filtering the export is a presentation choice, and
        # letting it shrink the state would make the next run report unchanged
        # products as new.
        if config.run.state_file and survivors:
            tracker.save(
                config.run.state_file,
                {row.key: row.product.content_hash for row in rows},
                now=finished,
            )
        elif config.run.state_file:
            self._log.warning(
                "state.not_written",
                reason="the run produced no products; keeping the previous state file",
                path=config.run.state_file,
            )

        report = RunReport(
            run_id=run_id,
            started_at=started,
            finished_at=finished,
            # Elapsed time is deliberately independent of the injectable data
            # clock. ``--now`` freezes relative-date interpretation but must not
            # turn a four-second browser run into a reported duration of zero.
            duration_seconds=round(duration_seconds, 3),
            config_path=config.source_path,
            config_sha256=config.source_sha256,
            state_file=config.run.state_file,
            sources=source_reports,
            failures=self._failures,
            duplicates=self._duplicates,
            rejections_by_reason=self._rejection_counts(),
            warnings=self._warnings,
            exported=len(exported_rows),
            changes=dict(change_counts) | {"filtered_out": filtered_out},
            outputs=outputs,
            exit_code=0 if not self._failures else 1,
        )
        if report.exported == 0:
            # An empty export is not automatically a bug -- a catalogue can be
            # empty -- but it is never something to report as success without
            # comment.
            report.exit_code = 1
            report.warnings.append(
                "no products were exported; check failures and rejections above"
            )

        # Reconcile the counters before anything is claimed to be finished. A
        # counting bug would otherwise ship as a plausible-looking report, which
        # is worse than a crash because nobody would ever check it.
        report.check()
        report.outputs = outputs | {"run-report.json": str(output_dir / "run-report.json")}
        write_report(output_dir, report)
        return report

    def _rejection_counts(self) -> dict[str, int]:
        counter: Counter[str] = Counter()
        for record in self._rejected:
            counter.update(record.reason_codes)
        return dict(sorted(counter.items()))

    def _log_summary(self, report: RunReport) -> None:
        totals = report.totals
        self._log.info(
            "run.finished",
            run_id=report.run_id,
            seconds=report.duration_seconds,
            pages=totals["pages_fetched"],
            pages_failed=totals["pages_failed"],
            found=totals["records_found"],
            exported=report.exported,
            rejected=totals["rejected"],
            duplicates=totals["duplicates_identical"] + totals["duplicates_conflicting"],
            exit_code=report.exit_code,
        )

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------

    def _fetcher_for(self, source: SourceConfig) -> Fetcher:
        if source.kind == "static":
            return self._http
        if self._browser is None:
            http = self._http
            assert isinstance(http, HttpFetcher), (
                "a browser source needs the real HTTP fetcher to share its robots policy"
            )
            self._browser = BrowserFetcher(
                self._config.browser,
                user_agent=self._config.http.user_agent,
                # Both fetchers share one throttle and one robots policy. A run
                # that used two transports with two independent rate limiters
                # would hit the target twice as fast as configured, which is
                # exactly the kind of politeness bug nobody notices until the
                # target notices it for them.
                throttle=http.throttle,
                robots=http.robots,
                log=self._log,
            )
        return self._browser

    def _close_fetchers(self) -> None:
        for fetcher in (self._browser, self._http):
            if fetcher is None:
                continue
            try:
                fetcher.close()
            except Exception as exc:
                self._log.warning("fetcher.close_failed", error=f"{type(exc).__name__}: {exc}")

    def _record_failure(
        self,
        source: SourceConfig,
        url: str,
        page_no: int,
        error: FetchError,
        report: SourceReport,
    ) -> None:
        failure = PageFailure(
            source_id=source.id,
            url=url,
            page_no=page_no,
            kind=error.kind,
            attempts=error.attempts,
            message=str(error),
        )
        self._failures.append(failure)
        report.pages_failed += 1
        self._log.error(
            "page.failed",
            source=source.id,
            page=page_no,
            url=url,
            kind=error.kind.value,
            attempts=error.attempts,
            reason=str(error),
        )

    def _add_warnings(self, warnings: list[str]) -> None:
        for warning in warnings:
            self._warning_count += 1
            if len(self._warnings) < MAX_REPORTED_WARNINGS:
                self._warnings.append(warning)
            elif len(self._warnings) == MAX_REPORTED_WARNINGS:
                self._warnings.append(
                    "... further warnings suppressed (see the log for all of them)"
                )


def _first_url(source: SourceConfig) -> str:
    if source.pagination.strategy == "page_param":
        assert source.pagination.page_param is not None
        return _with_page_param(
            source.start_url, source.pagination.page_param, source.pagination.start_page
        )
    return source.start_url


def _with_page_param(url: str, param: str, page: int) -> str:
    """Set ``?param=page`` on ``url``, replacing any existing value.

    Replacing rather than appending: ``?page=1&page=2&page=3`` is accepted by
    some servers and ignored by others, and the failure is silent — the same
    page is fetched repeatedly and every record after the first page is a
    duplicate.
    """
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != param]
    query.append((param, str(page)))
    return urlunsplit(parts._replace(query=urlencode(query)))


def utc_now() -> datetime:
    return datetime.now(UTC)
