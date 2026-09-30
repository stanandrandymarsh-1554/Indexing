"""Search Amazon by driving a real Chrome window on your own computer.

Amazon increasingly answers plain scripts with a "check you're human" page instead of
results, while serving normal browsers as usual. This backend loads the same search pages
in Chrome (via Playwright), at the same unhurried pace as the `web` backend, and reads
them with the same parser.

The window is visible by default. If Amazon asks you to confirm you're human, do it in
that window and the search carries on. Nothing here tries to hide that it's automated or
to solve checks by itself. The browser keeps its own profile (separate from your normal
Chrome), so cookies and any check you've passed are remembered between runs.

Needs: `pip3 install playwright`. It uses your installed Google Chrome; without one, also
run `python3 -m playwright install chromium`.
"""

from __future__ import annotations

import atexit
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

from .amazon_web import _NO_RESULTS_RE, _RESULT_RE, _ROBOT_MARKERS, DEFAULT_MARKETPLACE, AmazonWebProvider
from .base import ProviderError

INSTALL_HELP = "Chrome mode needs Playwright. Install it once with:  pip3 install playwright"


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def default_profile_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "amazon_search" / "chrome-profile"


class AmazonBrowserProvider(AmazonWebProvider):
    hint = ""  # already the most robust option; the error messages stand on their own

    def __init__(
        self,
        marketplace: str = DEFAULT_MARKETPLACE,
        headless: bool = False,
        profile_dir: Optional[Path] = None,
        check_timeout: float = 180.0,
        executable_path: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        # The parent's urllib opener is never used: pages come from Chrome instead.
        super().__init__(marketplace=marketplace, opener=_no_opener, **kwargs)
        self.headless = headless
        self.profile_dir = Path(profile_dir) if profile_dir else default_profile_dir()
        self.check_timeout = check_timeout
        self.executable_path = executable_path or os.environ.get("AMAZON_SEARCH_CHROME")
        self._pw: Any = None
        self._context: Any = None
        self._page: Any = None
        self._told_about_check = False

    # ------------------------------------------------------------------ browser

    def _ensure_page(self) -> Any:
        if self._page is not None:
            return self._page
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise ProviderError(INSTALL_HELP) from e

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        atexit.register(self.close)
        options: dict[str, Any] = {"headless": self.headless, "locale": "en-GB" if ".co.uk" in self.host else "en-US"}
        attempts = (
            [{"executable_path": self.executable_path}]
            if self.executable_path
            else [{"channel": "chrome"}, {}]  # your installed Chrome first, else Playwright's Chromium
        )
        last_error: Optional[Exception] = None
        for extra in attempts:
            try:
                self._context = self._pw.chromium.launch_persistent_context(
                    str(self.profile_dir), **options, **extra
                )
                break
            except PlaywrightError as e:
                last_error = e
        if self._context is None:
            self.close()
            raise ProviderError(
                "Couldn't start Chrome. Install Google Chrome, or run:  python3 -m playwright install chromium"
                f"  (details: {str(last_error).splitlines()[0] if last_error else 'unknown'})"
            )
        self._context.set_default_timeout(self.timeout * 1000)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        return self._page

    def close(self) -> None:
        for obj, method in ((self._context, "close"), (self._pw, "stop")):
            if obj is not None:
                try:
                    getattr(obj, method)()
                except Exception:
                    pass  # the browser may already be gone (e.g. the user closed the window)
        self._context = self._pw = self._page = None

    # ------------------------------------------------------------------ fetching

    def _fetch(self, url: str) -> str:
        with self._lock:
            wait = self._last_request_at + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()
        page = self._ensure_page()
        try:
            page.goto(url, wait_until="domcontentloaded")
        except Exception as e:
            raise ProviderError(f"Chrome couldn't load {url}: {str(e).splitlines()[0]}") from e
        return self._wait_for_results(page)

    def _wait_for_results(self, page: Any) -> str:
        """Return the page once it shows results or "no results".

        Anything else is usually a check page. Real browsers often clear those by themselves
        within a few seconds; if not, and the window is visible, give the user time to do it.
        """
        # Headless: nobody can do a check, so only wait for ones that clear by themselves.
        deadline = time.monotonic() + (self.check_timeout if not self.headless else min(15.0, self.check_timeout))
        html = ""
        while True:
            try:
                html = page.content()
            except Exception:
                html = ""  # mid-navigation; try again
            if _RESULT_RE.search(html) or _NO_RESULTS_RE.search(html):
                return html
            if time.monotonic() >= deadline:
                return html  # search() will report it as blocked / unreadable
            low = html.lower()
            if not self.headless and not self._told_about_check and any(m in low for m in _ROBOT_MARKERS):
                print(
                    "Amazon wants to check you're human. Please complete the check in the Chrome "
                    f"window; waiting up to {int(self.check_timeout)} seconds…",
                    file=sys.stderr,
                    flush=True,
                )
                self._told_about_check = True
            time.sleep(1)


def _no_opener(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("AmazonBrowserProvider fetches pages with Chrome, not urllib")
