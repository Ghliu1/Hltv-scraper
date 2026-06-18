"""A persistent, Cloudflare-solving browser session for fetching HLTV pages.

HLTV serves a Cloudflare JavaScript/managed challenge that plain HTTP clients
(even with TLS impersonation) cannot pass from many IPs. A *real* browser solves
it transparently. The single most important anti-ban behaviour is implemented
here: launch **one** browser, solve the challenge **once** during warm-up, and
reuse that warmed session (its ``cf_clearance`` cookie) for every subsequent
page — exactly how a human browsing the site would look.

Backends, in preference order:
  * ``undetected-chromedriver`` — patches Selenium/Chrome automation tells; the
    most reliable against Cloudflare (and what comparable scrapers use).
  * plain Selenium Chrome — with anti-automation flags, as a fallback.

Both are imported lazily and guarded, so the package works without them
installed; the browser is only required when ``backend="browser"``.

Note: a real (non-headless) browser is the most evasive — Cloudflare fingerprints
headless Chrome — so headless defaults to *off*. On a headless server, run under
a virtual display (e.g. ``xvfb-run``).
"""

from __future__ import annotations

import logging
import time
from typing import Optional, Tuple

from .config import Settings

log = logging.getLogger("hltv")

# Markers that indicate a Cloudflare interstitial rather than real content.
_CHALLENGE_MARKERS = (
    "Just a moment",
    "Checking your browser",
    "cf-browser-verification",
    "challenge-platform",
    "Enable JavaScript and cookies to continue",
)


class BrowserUnavailable(RuntimeError):
    """Raised when no usable browser backend can be launched."""


class BrowserSession:
    """Owns a single long-lived browser and serves pages from it."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._driver = None
        self._driver_kind: Optional[str] = None

    # -- driver construction ----------------------------------------------
    def _build_driver(self):
        pref = self.settings.browser_driver  # auto | undetected | selenium
        headless = self.settings.browser_headless

        if pref in ("auto", "undetected"):
            try:
                return self._build_undetected(headless), "undetected"
            except Exception as exc:  # pragma: no cover - env dependent
                if pref == "undetected":
                    raise BrowserUnavailable(
                        f"undetected-chromedriver unavailable: {exc}") from exc
                log.warning("undetected-chromedriver unavailable (%s); "
                            "falling back to plain selenium", exc)

        try:
            return self._build_selenium(headless), "selenium"
        except Exception as exc:  # pragma: no cover - env dependent
            raise BrowserUnavailable(
                f"no usable browser backend could be launched: {exc}") from exc

    def _common_args(self):
        args = [
            "--disable-extensions",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-infobars",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--window-size=1280,900",
        ]
        return args

    def _build_undetected(self, headless: bool):
        import undetected_chromedriver as uc  # lazy, guarded
        options = uc.ChromeOptions()
        for a in self._common_args():
            options.add_argument(a)
        if self.settings.proxy:
            options.add_argument(f"--proxy-server={self.settings.proxy}")
        if self.settings.browser_binary:
            options.binary_location = self.settings.browser_binary
        return uc.Chrome(options=options, headless=headless)

    def _build_selenium(self, headless: bool):
        from selenium import webdriver  # lazy, guarded
        from selenium.webdriver.chrome.options import Options
        opts = Options()
        if headless:
            opts.add_argument("--headless=new")
        for a in self._common_args():
            opts.add_argument(a)
        if self.settings.proxy:
            opts.add_argument(f"--proxy-server={self.settings.proxy}")
        if self.settings.browser_binary:
            opts.binary_location = self.settings.browser_binary
        # Reduce the most obvious automation fingerprints.
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        driver = webdriver.Chrome(options=opts)
        try:
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator,'webdriver',"
                           "{get:()=>undefined})"},
            )
        except Exception:
            pass
        return driver

    def _ensure(self) -> None:
        if self._driver is not None:
            return
        self._driver, self._driver_kind = self._build_driver()
        log.info("browser session started (%s, headless=%s)",
                 self._driver_kind, self.settings.browser_headless)
        if self.settings.browser_warmup:
            self._warmup()

    # -- challenge handling -----------------------------------------------
    def _looks_like_challenge(self, source: str) -> bool:
        head = source[:4000]
        return any(m in head for m in _CHALLENGE_MARKERS)

    def _wait_for_clearance(self, max_wait: Optional[float] = None) -> bool:
        """Poll until the Cloudflare interstitial clears and real HTML loads."""
        deadline = time.time() + (max_wait or self.settings.browser_challenge_wait)
        while time.time() < deadline:
            try:
                source = self._driver.page_source or ""
            except Exception:
                source = ""
            if source and not self._looks_like_challenge(source) and len(source) > 2000:
                return True
            time.sleep(1.0)
        return False

    def _warmup(self) -> None:
        """Visit the homepage so Cloudflare issues a clearance cookie once."""
        try:
            self._driver.get(self.settings.base_url + "/")
        except Exception as exc:  # pragma: no cover
            log.warning("warmup navigation failed: %s", exc)
            return
        cleared = self._wait_for_clearance()
        log.info("browser warmup %s", "cleared" if cleared else
                 "did not clear (will retry per-page)")

    # -- public fetch ------------------------------------------------------
    def get(self, url: str) -> Tuple[int, str]:
        """Navigate to ``url`` and return (pseudo-status, html).

        Returns 200 when real content loads, 403 if the challenge persists — so
        the calling client's retry/backoff logic treats it like any block.
        """
        self._ensure()
        try:
            self._driver.get(url)
        except Exception as exc:  # navigation/timeout
            log.warning("browser navigation error for %s: %s", url, exc)
            return -1, ""
        ok = self._wait_for_clearance()
        try:
            source = self._driver.page_source or ""
        except Exception:
            source = ""
        if not ok or self._looks_like_challenge(source):
            return 403, source
        return 200, source

    def close(self) -> None:
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None
