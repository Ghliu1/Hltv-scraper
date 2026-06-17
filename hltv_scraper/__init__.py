"""hltv_scraper — a scraper + datastore for professional CS:GO/CS2 statistics from HLTV.

The package is organised into clear layers so each part can be tested and
maintained independently:

    config      configuration (rate limits, paths, URLs, periods)
    utils       small pure helpers (slugs, dates, numeric parsing)
    http_client Cloudflare-aware, rate-limited, disk-cached fetcher
    models      typed dataclasses describing every entity we persist
    db          SQLite schema + idempotent upserts
    parsers/    pure HTML -> dataclass functions (no network, easy to test)
    scrapers/   orchestration: fetch -> parse -> persist, across time periods
    analysis/   downstream modelling (player contribution, weapon/meta trends)

The deliberate split between `parsers` (pure) and `scrapers` (effectful) means
the fragile part — HLTV's HTML — is isolated and unit-testable against saved
fixtures, while network behaviour lives in one place.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
