import json
import os
import tempfile
import unittest
from pathlib import Path

from amazon_search.models import SearchFilters
from amazon_search.providers.amazon_browser import AmazonBrowserProvider, playwright_available
from amazon_search.providers.amazon_web import UnreadablePageError

FIXTURES = Path(__file__).parent / "fixtures"
# Point AMAZON_SEARCH_CHROME at a Chrome/Chromium binary if neither Google Chrome nor
# Playwright's own Chromium is installed where Playwright looks by default.
CHROME = os.environ.get("AMAZON_SEARCH_CHROME") or (
    "/opt/pw-browsers/chromium" if os.path.exists("/opt/pw-browsers/chromium") else None
)


@unittest.skipUnless(playwright_available(), "Playwright not installed")
class BrowserProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prov = AmazonBrowserProvider(
            headless=True, min_interval=0, profile_dir=Path(self.tmp.name) / "profile", executable_path=CHROME
        )
        self.addCleanup(self.prov.close)

    def serve(self, name_or_html):
        """Make search() load a local file instead of Amazon."""
        path = FIXTURES / name_or_html
        if not path.exists():
            path = Path(self.tmp.name) / "page.html"
            path.write_text(name_or_html)
        self.prov.search_url = lambda *a, **k: path.as_uri()

    def test_reads_results_rendered_in_chrome(self):
        self.serve("amazon_search_page.html")
        products = self.prov.search("wireless headphones", SearchFilters())
        self.assertEqual([p.asin for p in products], ["B0TEST0001", "B0TEST0002", "B0TEST0003"])
        self.assertEqual(products[0].price, 79.99)

    def test_results_added_by_javascript_are_seen(self):
        # A check page that turns into results after a moment, as real browsers experience.
        page = (FIXTURES / "amazon_search_page.html").read_text()
        body = page.split("<body>", 1)[1].split("</body>", 1)[0]
        self.serve(
            "<html><body><p>One moment…</p><script>setTimeout(function(){document.body.innerHTML = "
            + json.dumps(body).replace("</", "<\\/")
            + ";}, 1500);</script></body></html>"
        )
        self.assertEqual(len(self.prov.search("x", SearchFilters())), 3)

    def test_page_that_never_shows_results_is_reported(self):
        self.prov.check_timeout = 2
        cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, cwd)
        self.serve("<html><body>Checking your browser before accessing Amazon</body></html>")
        with self.assertRaises(UnreadablePageError):
            self.prov.search("x", SearchFilters())

    def test_profile_is_kept_between_runs(self):
        self.serve("amazon_search_page.html")
        self.prov.search("x", SearchFilters())
        self.prov.close()
        self.assertTrue(any((Path(self.tmp.name) / "profile").iterdir()))


if __name__ == "__main__":
    unittest.main()
