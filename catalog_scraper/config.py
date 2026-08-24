"""Loading and validating the run configuration.

The configuration file is the interface a client actually edits, so it is
treated as an interface: every key is validated, unknown keys are errors, and
every failure names the path that caused it.

Three decisions worth calling out.

**Unknown keys are rejected.** ``extra="forbid"`` on every model means
``timout_seconds: 5`` is a loud error rather than a silently ignored line that
leaves the default in force. A misconfigured timeout that appears to have been
applied is exactly the class of bug that only shows up in production.

**Environment variables are expanded, with mandatory defaults or a loud
failure.** ``${DEMO_BASE_URL:-http://127.0.0.1:8800}`` works;
``${DEMO_BASE_URL}`` with nothing set raises :class:`ConfigError` naming the
variable. An unset variable must never expand to an empty string and produce a
URL like ``/catalog/page-1.html``.

**The configuration cannot contradict the data model.** A source that does not
map every required field, or maps a field that does not exist, fails here — at
load time, before any request is made. See :mod:`catalog_scraper.fields`.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import soupsieve
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from catalog_scraper.errors import ConfigError
from catalog_scraper.fields import FIELDS_BY_NAME, REQUIRED_FIELDS
from catalog_scraper.normalize import DateOrder, DecimalSeparator

SUPPORTED_VERSION = 1


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HttpSettings(_Base):
    """Fetch policy for static sources.

    Defaults are intentionally polite rather than fast: this is a portfolio
    project that a stranger may point at a real site.
    """

    timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 10.0
    max_attempts: Annotated[int, Field(ge=1, le=10)] = 3
    backoff_base_seconds: Annotated[float, Field(ge=0, le=60)] = 0.5
    backoff_max_seconds: Annotated[float, Field(ge=0, le=600)] = 30.0
    delay_seconds: Annotated[float, Field(ge=0, le=60)] = 0.5
    """Minimum spacing between requests to the same source."""
    user_agent: Annotated[str, Field(min_length=5)] = (
        "catalog-scrape-pipeline/1.0 (+https://github.com/yuten0901/catalog-scrape-pipeline)"
    )
    respect_robots: bool = True
    """Fetch and honour ``robots.txt`` before touching a host. See docs/ethics."""

    @model_validator(mode="after")
    def _backoff_bounds(self) -> Self:
        if self.backoff_max_seconds < self.backoff_base_seconds:
            raise ValueError("backoff_max_seconds must be >= backoff_base_seconds")
        return self


class BrowserSettings(_Base):
    """Playwright policy. Only consulted by sources with ``kind: browser``."""

    enabled: bool = True
    timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 20.0
    headless: bool = True


class DedupeSettings(_Base):
    key_fields: Annotated[list[str], Field(min_length=1)] = Field(default_factory=lambda: ["sku"])
    on_conflict: Literal["keep_first", "keep_last"] = "keep_first"
    """Which record survives when two share a key but disagree on content.

    ``keep_first`` follows listing order, which on a paginated catalogue is the
    site's own notion of canonical. Either way the conflict is reported.
    """

    @model_validator(mode="after")
    def _known_fields(self) -> Self:
        unknown = [name for name in self.key_fields if name not in FIELDS_BY_NAME]
        if unknown:
            raise ValueError(
                f"dedupe.key_fields refers to unknown product fields: {', '.join(unknown)}. "
                f"Known fields: {', '.join(sorted(FIELDS_BY_NAME))}"
            )
        return self


class RunSettings(_Base):
    output_dir: str = "out"
    formats: Annotated[list[Literal["csv", "json"]], Field(min_length=1)] = Field(
        default_factory=lambda: ["csv", "json"]
    )
    state_file: str | None = None
    """Enables change detection when set. ``None`` means every product reports
    ``change=unknown`` — which is not the same as ``new``."""
    log_format: Literal["text", "json"] = "text"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    csv_encoding: Literal["utf-8-sig", "utf-8"] = "utf-8-sig"
    """``utf-8-sig`` writes a BOM so Excel does not decode ``£`` as ``Â£``.
    See :func:`catalog_scraper.export._write_csv`."""


class FieldMapping(_Base):
    """Where one field lives inside a record element."""

    selector: Annotated[str, Field(min_length=1)]
    attr: str | None = None
    """Read this attribute instead of the element's text.

    Needed more often than one would like: ratings are commonly encoded as a CSS
    class (``class="star-rating Three"``) and machine-readable dates as
    ``<time datetime="...">``.
    """


class PaginationSettings(_Base):
    strategy: Literal["none", "next_link", "page_param"] = "none"
    next_selector: str | None = None
    page_param: str | None = None
    start_page: Annotated[int, Field(ge=1)] = 1
    max_pages: Annotated[int, Field(ge=1, le=1000)] = 20
    """A hard stop. Unbounded pagination against a site with a pagination bug is
    how a polite scraper becomes an accidental denial of service."""

    @model_validator(mode="after")
    def _strategy_needs_its_parameter(self) -> Self:
        if self.strategy == "next_link" and not self.next_selector:
            raise ValueError("pagination.strategy 'next_link' requires 'next_selector'")
        if self.strategy == "page_param" and not self.page_param:
            raise ValueError("pagination.strategy 'page_param' requires 'page_param'")
        return self


class SourceConfig(_Base):
    id: Annotated[str, Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")]
    kind: Literal["static", "browser"] = "static"
    start_url: Annotated[str, Field(min_length=1)]
    record_selector: Annotated[str, Field(min_length=1)]
    fields: dict[str, FieldMapping]
    pagination: PaginationSettings = PaginationSettings()
    date_order: DateOrder = DateOrder.DMY
    decimal_separator: DecimalSeparator = DecimalSeparator.AUTO
    """Resolves `£7.505`-shaped ambiguity. See :class:`DecimalSeparator`."""
    default_currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None
    wait_for_selector: str | None = None
    """Browser sources only: the selector whose appearance means "rendered"."""

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if not self.start_url.startswith(("http://", "https://")):
            raise ValueError(f"start_url must be an absolute http(s) URL, got {self.start_url!r}")

        unknown = sorted(set(self.fields) - set(FIELDS_BY_NAME))
        if unknown:
            raise ValueError(
                f"maps unknown fields {', '.join(unknown)}; "
                f"known fields are {', '.join(sorted(FIELDS_BY_NAME))}"
            )

        missing = sorted(REQUIRED_FIELDS - set(self.fields))
        if missing:
            raise ValueError(
                f"does not map required field(s) {', '.join(missing)}. Every product needs "
                f"them, so a source that cannot supply them would export empty columns."
            )

        # Every selector is compiled here, at load time. A typo in a CSS
        # selector is otherwise discovered on the first page of the first
        # source, after the browser has started and several requests have been
        # made to someone else's server, and only if that particular selector is
        # reached. This is the cheapest possible place to find it.
        _check_selector(self.record_selector, f"sources.{self.id}.record_selector")
        for name, mapping in self.fields.items():
            _check_selector(mapping.selector, f"sources.{self.id}.fields.{name}.selector")
        if self.pagination.next_selector:
            _check_selector(
                self.pagination.next_selector, f"sources.{self.id}.pagination.next_selector"
            )
        if self.wait_for_selector:
            _check_selector(self.wait_for_selector, f"sources.{self.id}.wait_for_selector")

        if self.kind == "browser" and not self.wait_for_selector:
            raise ValueError(
                "browser sources must set 'wait_for_selector'; without it the page is scraped "
                "at an arbitrary point during rendering and the result is a race"
            )
        if self.kind == "static" and self.wait_for_selector:
            raise ValueError("'wait_for_selector' is meaningless for a static source")
        return self


def _check_selector(selector: str, location: str) -> None:
    """Compile a CSS selector now so a typo cannot survive until page one."""
    try:
        soupsieve.compile(selector)
    except Exception as exc:  # soupsieve raises several distinct types
        raise ValueError(f"{location}: {selector!r} is not a valid CSS selector ({exc})") from exc


class PipelineConfig(_Base):
    version: int = SUPPORTED_VERSION
    run: RunSettings = RunSettings()
    http: HttpSettings = HttpSettings()
    browser: BrowserSettings = BrowserSettings()
    dedupe: DedupeSettings = DedupeSettings()
    sources: Annotated[list[SourceConfig], Field(min_length=1)]

    # Set by the loader, not by the file.
    source_path: str = ""
    source_sha256: str = ""

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.version != SUPPORTED_VERSION:
            raise ValueError(
                f"unsupported config version {self.version}; this build understands "
                f"version {SUPPORTED_VERSION}"
            )
        seen: set[str] = set()
        for source in self.sources:
            if source.id in seen:
                raise ValueError(f"duplicate source id {source.id!r}")
            seen.add(source.id)
        return self

    def source(self, source_id: str) -> SourceConfig:
        for source in self.sources:
            if source.id == source_id:
                return source
        raise KeyError(source_id)


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: str, env: Mapping[str, str], *, path: str) -> str:
    """Expand ``${VAR}`` and ``${VAR:-default}`` inside a configuration string.

    ``${VAR}`` with ``VAR`` unset is an error, not an empty string. This is the
    whole point of the function: silently expanding to ``""`` turns
    ``${BASE}/catalog`` into ``/catalog``, which fails much later and much less
    clearly, if at all.
    """

    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if name in env:
            return env[name]
        if default is not None:
            return default
        raise ConfigError(
            f"{path}: environment variable ${{{name}}} is not set and has no default. "
            f"Use ${{{name}:-some-default}} if an unset value is acceptable."
        )

    return _ENV_PATTERN.sub(replace, value)


def _expand_tree(node: Any, env: Mapping[str, str], path: str) -> Any:
    if isinstance(node, str):
        return expand_env(node, env, path=path)
    if isinstance(node, dict):
        return {key: _expand_tree(value, env, f"{path}.{key}") for key, value in node.items()}
    if isinstance(node, list):
        return [_expand_tree(item, env, f"{path}[{index}]") for index, item in enumerate(node)]
    return node


def load_config(path: str | Path, env: Mapping[str, str] | None = None) -> PipelineConfig:
    """Read, expand and validate a configuration file.

    Raises :class:`ConfigError` for every failure mode, with the file path and
    the offending key in the message.
    """
    config_path = Path(path)
    try:
        raw_bytes = config_path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read config file {config_path}: {exc}") from exc

    try:
        document = yaml.safe_load(raw_bytes.decode("utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{config_path} is not valid YAML: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{config_path} is not valid UTF-8: {exc}") from exc

    if not isinstance(document, dict):
        raise ConfigError(f"{config_path}: expected a YAML mapping at the top level")

    expanded = _expand_tree(document, os.environ if env is None else env, str(config_path))

    try:
        config = PipelineConfig.model_validate(
            expanded
            | {
                "source_path": str(config_path),
                # Hashing the bytes on disk, before expansion, so the digest in
                # the run report identifies the file a reviewer can open.
                "source_sha256": hashlib.sha256(raw_bytes).hexdigest(),
            }
        )
    except ValidationError as exc:
        raise ConfigError(f"{config_path} is invalid:\n{_format_errors(exc)}") from exc
    return config


def _format_errors(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)
