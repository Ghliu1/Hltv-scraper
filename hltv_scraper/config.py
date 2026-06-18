"""Central configuration for the scraper.

Everything that a user might reasonably want to tune lives here and can be
overridden via environment variables (prefix ``HLTV_``) so the tool can be
driven from CI / containers without code edits.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional


BASE_URL = "https://www.hltv.org"

# CS:GO was released on 2012-08-21. HLTV's structured stats coverage effectively
# begins in 2012, so that is our default lower bound for "from CS:GO's release".
CSGO_RELEASE = date(2012, 8, 21)

# A rotating set of realistic desktop user agents. HLTV/Cloudflare fingerprint
# heavily; pairing these with the curl_cffi browser-impersonation backend gives
# the best odds of getting through.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 "
    "Firefox/125.0",
]


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, str(default))).expanduser()


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    """Runtime settings. Construct via :meth:`from_env` for env-var overrides."""

    base_url: str = BASE_URL

    # --- Storage -----------------------------------------------------------
    data_dir: Path = field(default_factory=lambda: Path("data"))
    db_path: Path = field(default_factory=lambda: Path("data/hltv.sqlite3"))
    cache_dir: Path = field(default_factory=lambda: Path("data/cache"))

    # --- Politeness / rate limiting ---------------------------------------
    # HLTV is aggressive about blocking. These defaults are intentionally slow.
    min_delay: float = 8.0          # minimum seconds between requests
    max_delay: float = 16.0         # jitter upper bound
    max_retries: int = 5            # per-URL retry attempts
    backoff_base: float = 4.0       # exponential backoff base seconds
    timeout: float = 30.0           # per-request timeout (seconds)
    cache_ttl_days: int = 30        # reuse cached HTML newer than this

    # --- Scope ------------------------------------------------------------
    # Tier 1-3 is approximated by "teams ranked in the top N". 100 captures
    # essentially all tier 1-3 organisations at any given snapshot.
    top_n_teams: int = 100
    ranking_filter: str = "Top50"   # HLTV stats rankingFilter param
    start_date: date = CSGO_RELEASE

    # --- Backend selection ------------------------------------------------
    # Order in which HTTP backends are attempted. Valid: curl_cffi, cloudscraper,
    # requests. The first importable one wins unless a single backend is forced.
    backend: str = "auto"

    # Optional HTTP(S) proxy, e.g. "http://user:pass@host:port". HLTV blocks
    # datacenter IPs, so a residential/rotating proxy is usually required to
    # scrape at scale. Set via HLTV_PROXY.
    proxy: Optional[str] = None

    # --- Browser backend (recommended for real scraping) ------------------
    # Used only when backend == "browser". A real browser solves Cloudflare's
    # JS challenge; the session is warmed once and reused (the key anti-ban win).
    browser_driver: str = "auto"          # auto | undetected | selenium
    browser_headless: bool = False        # headless is more detectable: keep off
    browser_warmup: bool = True           # solve the challenge once up front
    browser_challenge_wait: float = 25.0  # max seconds to wait for clearance
    browser_binary: Optional[str] = None  # explicit Chrome/Chromium path

    # --- Long-run pacing (ban avoidance) ----------------------------------
    # Beyond per-request delay, take a longer breather periodically so a
    # multi-day, full-history scrape looks like intermittent human browsing.
    requests_per_break: int = 40          # 0 disables periodic breaks
    break_seconds: float = 90.0

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = _env_path("HLTV_DATA_DIR", Path("data"))
        return cls(
            base_url=os.environ.get("HLTV_BASE_URL", BASE_URL),
            data_dir=data_dir,
            db_path=_env_path("HLTV_DB_PATH", data_dir / "hltv.sqlite3"),
            cache_dir=_env_path("HLTV_CACHE_DIR", data_dir / "cache"),
            min_delay=_env_float("HLTV_MIN_DELAY", 8.0),
            max_delay=_env_float("HLTV_MAX_DELAY", 16.0),
            max_retries=_env_int("HLTV_MAX_RETRIES", 5),
            backoff_base=_env_float("HLTV_BACKOFF_BASE", 4.0),
            timeout=_env_float("HLTV_TIMEOUT", 30.0),
            cache_ttl_days=_env_int("HLTV_CACHE_TTL_DAYS", 30),
            top_n_teams=_env_int("HLTV_TOP_N_TEAMS", 100),
            ranking_filter=os.environ.get("HLTV_RANKING_FILTER", "Top50"),
            backend=os.environ.get("HLTV_BACKEND", "auto"),
            proxy=os.environ.get("HLTV_PROXY") or None,
            browser_driver=os.environ.get("HLTV_BROWSER_DRIVER", "auto"),
            browser_headless=_env_bool("HLTV_BROWSER_HEADLESS", False),
            browser_warmup=_env_bool("HLTV_BROWSER_WARMUP", True),
            browser_challenge_wait=_env_float("HLTV_BROWSER_WAIT", 25.0),
            browser_binary=os.environ.get("HLTV_BROWSER_BINARY") or None,
            requests_per_break=_env_int("HLTV_REQUESTS_PER_BREAK", 40),
            break_seconds=_env_float("HLTV_BREAK_SECONDS", 90.0),
        )

    def ensure_dirs(self) -> None:
        # Coerce in case paths were assigned as plain strings.
        self.data_dir = Path(self.data_dir)
        self.cache_dir = Path(self.cache_dir)
        self.db_path = Path(self.db_path)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
