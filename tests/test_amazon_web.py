import gzip
import io
import unittest
import urllib.error
import urllib.parse
from email.message import Message
from pathlib import Path

from amazon_search import Lexicon, SearchEngine
from amazon_search.models import SearchFilters
from amazon_search.providers.amazon_web import AmazonWebProvider, RobotCheckError, parse_search_page

PAGE = (Path(__file__).parent / "fixtures" / "amazon_search_page.html").read_text()
NO_RESULTS = '<html><body><span>No results for </span><span class="a-text-bold">zzqx.</span></body></html>'
CAPTCHA = (
    '<html><body><form action="/errors/validateCaptcha"><h4>Enter the characters you see below</h4>'
    "</form></body></html>"
)


class _Resp(io.BytesIO):
    def __init__(self, body: bytes, encoding: str = ""):
        super().__init__(body)
        self.headers = Message()
        self.headers["Content-Type"] = "text/html; charset=utf-8"
        if encoding:
            self.headers["Content-Encoding"] = encoding

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class FakeOpener:
    def __init__(self, *pages):
        self.pages = list(pages)
        self.urls = []

    def __call__(self, req, timeout=None):
        self.urls.append(req.full_url)
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page if isinstance(page, _Resp) else _Resp(page.encode())


def provider(*pages, **kw):
    opener = FakeOpener(*pages)
    return AmazonWebProvider(min_interval=0, opener=opener, **kw), opener


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.products = {p.asin: p for p in parse_search_page(PAGE)}

    def test_finds_results_and_skips_sponsored_and_duplicates(self):
        self.assertEqual(list(self.products), ["B0TEST0001", "B0TEST0002", "B0TEST0003"])
        with_ads = parse_search_page(PAGE, include_sponsored=True)
        self.assertEqual(with_ads[0].asin, "B0SPONSOR1")
        self.assertEqual(with_ads[0].title, "Cheap Headphones Deluxe")

    def test_fields(self):
        p = self.products["B0TEST0001"]
        self.assertEqual(p.title, "Wireless Noise Cancelling Over-Ear Headphones, 40H Battery")
        self.assertEqual(p.url, "https://www.amazon.com/dp/B0TEST0001")
        self.assertEqual((p.price, p.currency, p.display_price), (79.99, "USD", "$79.99"))  # not the struck-out $99.99
        self.assertEqual((p.rating, p.review_count), (4.5, 12840))
        self.assertEqual(p.image_url, "https://m.media-amazon.com/images/I/test1.jpg")

    def test_abbreviated_counts_list_price_first_and_thousands(self):
        p = self.products["B0TEST0002"]
        self.assertEqual((p.price, p.rating, p.review_count), (1029.5, 4.3, 20300))

    def test_missing_fields_are_none(self):
        p = self.products["B0TEST0003"]
        self.assertEqual((p.title, p.price, p.rating, p.review_count), ("Wired Studio Headphones", None, None, None))


class ProviderTests(unittest.TestCase):
    def test_url_includes_filters(self):
        prov, _ = provider()
        url = prov.search_url(
            "usb c cable", SearchFilters(search_index="Electronics", min_price=5, max_price=20, sort_by="Price:LowToHigh"), 2
        )
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertTrue(url.startswith("https://www.amazon.com/s?"))
        self.assertEqual(q, {"k": ["usb c cable"], "i": ["electronics"], "rh": ["p_36:500-2000"], "s": ["price-asc-rank"], "page": ["2"]})

    def test_open_ended_price_and_other_marketplace(self):
        prov = AmazonWebProvider(marketplace="https://www.amazon.co.uk/", min_interval=0)
        url = prov.search_url("kettle", SearchFilters(max_price=30))
        self.assertTrue(url.startswith("https://www.amazon.co.uk/s?"))
        self.assertIn("rh=p_36%3A-3000", url)

    def test_search_parses_gzip_page(self):
        prov, opener = provider(_Resp(gzip.compress(PAGE.encode()), "gzip"))
        products = prov.search("wireless headphones", SearchFilters())
        self.assertEqual(len(products), 3)
        self.assertIn("k=wireless+headphones", opener.urls[0])

    def test_rating_and_price_filters_are_enforced_locally(self):
        prov, _ = provider(PAGE)
        products = prov.search("headphones", SearchFilters(min_rating=4, max_price=100))
        self.assertEqual([p.asin for p in products], ["B0TEST0001"])

    def test_no_results_page_is_empty(self):
        prov, _ = provider(NO_RESULTS)
        self.assertEqual(prov.search("zzqx", SearchFilters()), [])

    def test_robot_check_stops_all_further_requests(self):
        prov, opener = provider(CAPTCHA, PAGE)
        with self.assertRaises(RobotCheckError):
            prov.search("a", SearchFilters())
        with self.assertRaises(RobotCheckError):
            prov.search("b", SearchFilters())
        self.assertEqual(len(opener.urls), 1)  # the second search never hit the network

    def test_http_503_is_a_robot_check(self):
        err = urllib.error.HTTPError("https://www.amazon.com/s", 503, "busy", {}, io.BytesIO(b""))
        prov, _ = provider(err)
        with self.assertRaises(RobotCheckError):
            prov.search("a", SearchFilters())


class EngineWithWebProviderTests(unittest.TestCase):
    def test_partial_results_survive_a_block(self):
        # The exact query works, then Amazon blocks during the related-term searches.
        prov, _ = provider(PAGE, CAPTCHA)
        report = SearchEngine(prov, Lexicon.load(), pages=1).search("wireless headphones")
        self.assertEqual(len(report.results), 3)
        self.assertTrue(any(n.startswith("Stopped early") for n in report.notes))

    def test_block_with_nothing_found_raises(self):
        prov, _ = provider(CAPTCHA)
        with self.assertRaises(RobotCheckError):
            SearchEngine(prov, Lexicon.load()).search("wireless headphones")


if __name__ == "__main__":
    unittest.main()
