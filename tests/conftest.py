from __future__ import annotations

from collections.abc import Iterator

import pytest

from demo_site.server import DemoSite


@pytest.fixture
def demo_site() -> Iterator[DemoSite]:
    with DemoSite() as site:
        yield site
