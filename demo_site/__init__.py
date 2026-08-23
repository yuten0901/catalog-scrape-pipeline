"""A self-hosted demo website: the scraping target that ships with the project.

See :mod:`demo_site.server` for the routes and for why the target is local.
"""

from demo_site.server import DemoSite, FaultPolicy

__all__ = ["DemoSite", "FaultPolicy"]
