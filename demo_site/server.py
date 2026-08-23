"""A small, deliberately hostile website, served from this repository.

The pipeline needs a target that (a) exists forever, (b) behaves identically on
every machine, and (c) can be made to fail on demand. A public practice site
gives you none of those: it changes, it rate-limits differently from one country
to the next, and you cannot ask it to return a 503 twice and then recover.

So the demo target lives here. It is a real HTTP server — the tests make real
``httpx`` requests and drive a real browser against it, rather than stubbing the
transport and hoping the stub resembles the network. That distinction matters:
a mocked transport cannot catch a wrong timeout unit or a missing
``Retry-After`` header, and both of those are real bugs.

Routes
------
``/robots.txt``                 allows everything except ``/private/``
``/catalog/page-{1..4}.html``   the paginated static catalogue
``/catalog/redesigned.html``    HTTP 200, healthy markup, zero matching records
``/js/catalog.html?page=N``     client-rendered; empty without a browser
``/flaky/mirror.html``          503, 503, then 200 -- retry recovery
``/limited/mirror.html``        429 + ``Retry-After``, then 200 -- rate limiting
``/slow/mirror.html``           sleeps, so a client timeout can be observed
``/private/page-1.html``        serves fine, but robots.txt forbids it
anything else                   404

Fault state lives on the instance and is reset by :meth:`DemoSite.reset`, so a
test can assert "the second attempt succeeded" and the next test still gets two
failures.
"""

from __future__ import annotations

import argparse
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PAGES = Path(__file__).parent / "pages"

# Route -> file on disk. Everything not in here (and not a fault route) is a 404.
_STATIC_ROUTES: dict[str, str] = {
    "/robots.txt": "robots.txt",
    "/catalog/page-1.html": "catalog-page-1.html",
    "/catalog/page-2.html": "catalog-page-2.html",
    "/catalog/page-3.html": "catalog-page-3.html",
    "/catalog/page-4.html": "catalog-page-4.html",
    "/catalog/redesigned.html": "catalog-redesigned.html",
    "/js/catalog.html": "js-catalog.html",
    "/private/page-1.html": "catalog-page-1.html",
}


@dataclass
class FaultPolicy:
    """How many times each fault route misbehaves before it recovers.

    Defaults are chosen so that the shipped configuration succeeds: the flaky
    route fails twice and ``http.max_attempts`` is 3. A demo whose headline run
    is red teaches the reader nothing about retries — it just looks broken.
    """

    flaky_failures: int = 2
    """503s served by ``/flaky/`` before it gives up and returns the page."""

    rate_limited_failures: int = 1
    """429s served by ``/limited/``. Each carries ``Retry-After: 1``."""

    slow_seconds: float = 5.0
    """How long ``/slow/`` stalls. Must exceed the timeout under test."""

    retry_after_seconds: int = 1

    counters: dict[str, int] = field(default_factory=dict)
    """Requests seen per path, including the ones that were failed."""


class _Handler(BaseHTTPRequestHandler):
    server_version = "DemoSite/1.0"

    # Set on the server object by DemoSite.
    site: DemoSite

    def do_GET(self) -> None:  # noqa: N802  (BaseHTTPRequestHandler's API)
        parsed = urlsplit(self.path)
        path = parsed.path
        site = self.site
        site.policy.counters[path] = site.policy.counters.get(path, 0) + 1
        seen = site.policy.counters[path]

        if path.startswith("/flaky/"):
            if seen <= site.policy.flaky_failures:
                self._send_error(503, "upstream temporarily unavailable")
                return
            self._send_file("catalog-mirror.html")
            return

        if path.startswith("/limited/"):
            if seen <= site.policy.rate_limited_failures:
                self._send_error(
                    429,
                    "slow down",
                    extra_headers={"Retry-After": str(site.policy.retry_after_seconds)},
                )
                return
            self._send_file("catalog-mirror.html")
            return

        if path.startswith("/slow/"):
            time.sleep(site.policy.slow_seconds)
            self._send_file("catalog-mirror.html")
            return

        filename = _STATIC_ROUTES.get(path)
        if filename is None:
            self._send_error(404, f"no such page: {path}")
            return

        # The JS catalogue is one file that renders different rows per ?page=.
        # Serving it for any page number (including ones with no rows) is what
        # lets the page_param strategy discover the end of the catalogue.
        if path == "/js/catalog.html":
            parse_qs(parsed.query)  # parsed for realism; the page reads it itself
        self._send_file(filename)

    def _send_file(self, filename: str) -> None:
        body = (PAGES / filename).read_bytes()
        content_type = "text/plain; charset=utf-8" if filename.endswith(".txt") else "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(
        self, status: int, message: str, extra_headers: dict[str, str] | None = None
    ) -> None:
        body = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Silence the default stderr access log.

        The pipeline's own structured log is the thing under observation during a
        run; interleaving a second, differently-formatted log makes the output
        unreadable and the screenshots useless.
        """


class DemoSite:
    """Runs :mod:`demo_site` on an ephemeral port in a background thread."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0, policy: FaultPolicy | None = None):
        self.policy = policy or FaultPolicy()
        handler = type("_BoundHandler", (_Handler,), {"site": self})
        self._server = ThreadingHTTPServer((host, port), handler)
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[0], self._server.server_address[1]
        return f"http://{host}:{port}"

    def start(self) -> str:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def reset(self) -> None:
        """Forget every fault counter, so the next test starts from a clean site."""
        self.policy.counters.clear()

    def hits(self, path: str) -> int:
        """How many requests reached ``path``, including failed ones.

        Retry tests assert on this rather than on log lines: the point of a
        retry is that the server was contacted again, and only the server can
        confirm that.
        """
        return self.policy.counters.get(path, 0)

    def __enter__(self) -> DemoSite:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the demo catalogue site.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8800)
    args = parser.parse_args()

    site = DemoSite(host=args.host, port=args.port)
    url = site.start()
    print(f"demo site listening on {url}")
    print(f"  static catalogue : {url}/catalog/page-1.html")
    print(f"  rendered catalogue: {url}/js/catalog.html")
    print("press Ctrl-C to stop")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        site.stop()


if __name__ == "__main__":
    main()
