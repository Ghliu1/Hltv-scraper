"""Cloudflare-aware, rate-limited, disk-cached HTTP client for HLTV.

HLTV sits behind Cloudflare and is hostile to scrapers, so this client:

* tries the strongest available backend first — ``curl_cffi`` (real browser TLS
  fingerprints), then ``cloudscraper``, then plain ``requests``;
* enforces a randomised minimum delay between requests (politeness + evasion);
* retries with exponential backoff on 403/429/5xx and transient errors;
* caches every successful response body to disk keyed by URL, so re-parsing or
  resuming never re-hits the network (and you can develop parsers offline).

Nothing here knows about HLTV's HTML — it just returns text.
"""

from __future__ import annotations

import hashlib
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .config import Settings, USER_AGENTS

# Backend availability is probed lazily so the package imports without them.
try:  # pragma: no cover - import guard
    from curl_cffi import requests as curl_requests  # type: ignore
    _HAS_CURL = True
except Exception:  # pragma: no cover
    _HAS_CURL = False

try:  # pragma: no cover - import guard
    import cloudscraper  # type: ignore
    _HAS_CLOUDSCRAPER = True
except Exception:  # pragma: no cover
    _HAS_CLOUDSCRAPER = False

import requests as _requests


class FetchError(RuntimeError):
    """Raised when a URL cannot be retrieved after all retries."""


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]


class HltvClient:
    """Fetch HLTV pages politely, with caching and Cloudflare evasion."""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or Settings()
        self.settings.ensure_dirs()
        self._last_request = 0.0
        self._session = None
        self._browser = None
        self._request_count = 0
        self.backend_name = self._select_backend()

    # -- backend selection -------------------------------------------------
    def _select_backend(self) -> str:
        choice = self.settings.backend
        # The browser backend is never auto-selected (it needs Chrome + a
        # display); it must be requested explicitly via HLTV_BACKEND=browser.
        if choice == "browser":
            from .browser import BrowserSession
            self._browser = BrowserSession(self.settings)
            return "browser"
        order = (
            [choice]
            if choice != "auto"
            else ["curl_cffi", "cloudscraper", "requests"]
        )
        for name in order:
            if name == "curl_cffi" and _HAS_CURL:
                return "curl_cffi"
            if name == "cloudscraper" and _HAS_CLOUDSCRAPER:
                self._session = cloudscraper.create_scraper(
                    browser={"browser": "chrome", "platform": "windows",
                             "mobile": False}
                )
                return "cloudscraper"
            if name == "requests":
                self._session = _requests.Session()
                return "requests"
        # Forced backend that isn't importable -> fall back to requests.
        self._session = _requests.Session()
        return "requests"

    # -- caching -----------------------------------------------------------
    def _cache_path(self, url: str) -> Path:
        return self.settings.cache_dir / f"{_cache_key(url)}.html"

    def _read_cache(self, url: str) -> Optional[str]:
        p = self._cache_path(url)
        if not p.exists():
            return None
        if self.settings.cache_ttl_days >= 0:
            age = datetime.now() - datetime.fromtimestamp(p.stat().st_mtime)
            if age > timedelta(days=self.settings.cache_ttl_days):
                return None
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            return None

    def _write_cache(self, url: str, text: str) -> None:
        try:
            self._cache_path(url).write_text(text, encoding="utf-8")
        except OSError:
            pass

    # -- rate limiting -----------------------------------------------------
    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request
        delay = random.uniform(self.settings.min_delay, self.settings.max_delay)
        if elapsed < delay:
            time.sleep(delay - elapsed)

        # Periodic longer breather so a full-history run looks like a human
        # browsing intermittently rather than a relentless crawler.
        self._request_count += 1
        per_break = self.settings.requests_per_break
        if per_break and self._request_count % per_break == 0:
            pause = random.uniform(self.settings.break_seconds,
                                   self.settings.break_seconds * 1.6)
            time.sleep(pause)

        self._last_request = time.time()

    def _headers(self) -> dict:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": self.settings.base_url + "/",
            "Upgrade-Insecure-Requests": "1",
        }

    # -- single raw fetch (no retry / no cache) ----------------------------
    def _proxies(self) -> Optional[dict]:
        if not self.settings.proxy:
            return None
        return {"http": self.settings.proxy, "https": self.settings.proxy}

    def _raw_get(self, url: str) -> tuple[int, str]:
        if self.backend_name == "browser":
            return self._browser.get(url)
        headers = self._headers()
        timeout = self.settings.timeout
        proxies = self._proxies()
        if self.backend_name == "curl_cffi":
            resp = curl_requests.get(
                url, headers=headers, timeout=timeout, impersonate="chrome124",
                proxies=proxies,
            )
            return resp.status_code, resp.text
        resp = self._session.get(url, headers=headers, timeout=timeout,
                                 proxies=proxies)
        return resp.status_code, resp.text

    # -- public API --------------------------------------------------------
    def get(self, url: str, *, use_cache: bool = True,
            force: bool = False) -> str:
        """Return the HTML for ``url``, honouring cache + retries.

        Raises :class:`FetchError` if the page cannot be retrieved.
        """
        if use_cache and not force:
            cached = self._read_cache(url)
            if cached is not None:
                return cached

        last_err: Optional[str] = None
        for attempt in range(1, self.settings.max_retries + 1):
            self._throttle()
            try:
                status, text = self._raw_get(url)
            except Exception as exc:  # network/TLS errors
                last_err = f"{type(exc).__name__}: {exc}"
                status, text = -1, ""

            if status == 200 and text and "Just a moment" not in text[:2000]:
                self._write_cache(url, text)
                return text

            if status in (403, 429, 503) or status == -1:
                # Cloudflare / rate-limit / transient: back off and retry.
                wait = self.settings.backoff_base * (2 ** (attempt - 1))
                wait += random.uniform(0, self.settings.backoff_base)
                last_err = last_err or f"HTTP {status}"
                if attempt < self.settings.max_retries:
                    time.sleep(wait)
                continue

            if status == 404:
                raise FetchError(f"404 Not Found: {url}")

            last_err = f"HTTP {status}"

        raise FetchError(f"Failed to fetch {url} after "
                         f"{self.settings.max_retries} attempts ({last_err})")

    def get_cached_only(self, url: str) -> Optional[str]:
        """Return cached HTML if present (never touches the network)."""
        return self._read_cache(url)

    def close(self) -> None:
        """Release the browser session (if any). Safe to call repeatedly."""
        if self._browser is not None:
            self._browser.close()
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
