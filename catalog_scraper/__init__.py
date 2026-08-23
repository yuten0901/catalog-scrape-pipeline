"""A configurable pipeline that collects catalogue data and reports its own gaps.

The public surface is deliberately small: build a :class:`~catalog_scraper.config.PipelineConfig`
with :func:`~catalog_scraper.config.load_config`, hand it to
:class:`~catalog_scraper.pipeline.Pipeline`, and read the returned
:class:`~catalog_scraper.models.RunReport`.
"""

__version__ = "1.0.0"
