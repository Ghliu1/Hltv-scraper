"""Orchestration layer: fetch -> parse -> persist.

Scrapers wire the (pure) parsers to the (effectful) HTTP client and database.
They own the *control flow* — which URLs to visit, in what order, across which
time periods — while delegating HTML understanding to :mod:`parsers` and
storage to :mod:`db`. Every scraper is resumable: it consults the scrape log
and skips URLs already completed.
"""

from .orchestrator import Orchestrator  # noqa: F401
