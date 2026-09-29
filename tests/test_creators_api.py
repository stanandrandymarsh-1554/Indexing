import io
import json
import unittest
import urllib.error
from unittest import mock

from amazon_search.models import SearchFilters
from amazon_search.providers.base import ProviderError
from amazon_search.providers.creators_api import API_URL, AUTH_URLS, CreatorsApiProvider, parse_item

ITEM = {
    "asin": "B000TEST01",
    "detailPageURL": "https://www.amazon.com/dp/B000TEST01?tag=mytag-20",
    "itemInfo": {
        "title": {"displayValue": "Stainless Steel Water Bottle"},
        "byLineInfo": {"brand": {"displayValue": "Acme"}},
        "features": {"displayValues": ["Keeps cold 24h"]},
        "classifications": {"productGroup": {"displayValue": "Kitchen"}},
    },
    "offersV2": {"listings": [{"price": {"money": {"amount": 24.5, "currency": "USD", "displayAmount": "$24.50"}}}]},
    "customerReviews": {"count": 1234, "starRating": {"value": 4.6}},
    "images": {"primary": {"medium": {"url": "https://m.media-amazon.com/images/I/x.jpg"}}},
}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _http_error(url, code, body):
    return urllib.error.HTTPError(url, code, "err", {}, io.BytesIO(json.dumps(body).encode()))


class FakeOpener:
    def __init__(self, api_responses):
        self.api_responses = list(api_responses)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if req.full_url in AUTH_URLS.values():
            return _Resp(json.dumps({"access_token": "tok123", "expires_in": 3600}).encode())
        resp = self.api_responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return _Resp(json.dumps(resp).encode())


def make(opener, version="3.1"):
    return CreatorsApiProvider("cid", "secret", "mytag-20", version=version, min_interval=0, opener=opener)


class CreatorsApiTests(unittest.TestCase):
    def test_search_builds_request_and_parses_items(self):
        opener = FakeOpener([{"searchResult": {"items": [ITEM]}}])
        filters = SearchFilters(search_index="HomeAndKitchen", max_price=30, min_rating=4, sort_by="Relevance")
        products = make(opener).search("water bottle", filters, page=2)

        token_req, api_req = opener.requests
        self.assertEqual(token_req.full_url, AUTH_URLS["3.1"])
        self.assertEqual(json.loads(token_req.data)["scope"], "creatorsapi::default")
        self.assertEqual(api_req.full_url, f"{API_URL}/searchItems")
        self.assertEqual(api_req.get_header("Authorization"), "Bearer tok123")
        self.assertEqual(api_req.get_header("X-marketplace"), "www.amazon.com")
        body = json.loads(api_req.data)
        self.assertEqual(body["keywords"], "water bottle")
        self.assertEqual(body["partnerTag"], "mytag-20")
        self.assertEqual(body["itemPage"], 2)
        self.assertEqual(body["searchIndex"], "HomeAndKitchen")
        self.assertEqual(body["maxPrice"], 3000)  # lowest currency denomination
        self.assertEqual(body["minReviewsRating"], 4)
        self.assertIn("itemInfo.title", body["resources"])

        (p,) = products
        self.assertEqual((p.asin, p.title, p.brand, p.price, p.rating, p.review_count), ("B000TEST01", "Stainless Steel Water Bottle", "Acme", 24.5, 4.6, 1234))
        self.assertEqual(p.display_price, "$24.50")

    def test_token_is_reused(self):
        opener = FakeOpener([{"searchResult": {"items": []}}, {"searchResult": {"items": []}}])
        prov = make(opener)
        prov.search("a", SearchFilters())
        prov.search("b", SearchFilters())
        self.assertEqual(sum(r.full_url in AUTH_URLS.values() for r in opener.requests), 1)

    def test_cognito_credentials_use_versioned_header(self):
        opener = FakeOpener([{"searchResult": {"items": []}}])
        make(opener, version="2.1").search("a", SearchFilters())
        token_req, api_req = opener.requests
        self.assertTrue(token_req.get_header("Authorization").startswith("Basic "))
        self.assertEqual(api_req.get_header("Authorization"), "Bearer tok123, Version 2.1")

    def test_no_results_error_is_an_empty_list(self):
        err = _http_error(f"{API_URL}/searchItems", 404, {"errors": [{"code": "NoResults", "message": "none"}]})
        self.assertEqual(make(FakeOpener([err])).search("zzz", SearchFilters()), [])

    def test_throttling_is_retried(self):
        err = _http_error(f"{API_URL}/searchItems", 429, {"message": "Too many requests"})
        opener = FakeOpener([err, {"searchResult": {"items": [ITEM]}}])
        with mock.patch("amazon_search.providers.creators_api.time.sleep"):
            self.assertEqual(len(make(opener).search("x", SearchFilters())), 1)

    def test_auth_failure_raises_provider_error(self):
        err = _http_error(f"{API_URL}/searchItems", 403, {"errors": [{"code": "AccessDenied", "message": "nope"}]})
        with self.assertRaisesRegex(ProviderError, "AccessDenied"):
            make(FakeOpener([err])).search("x", SearchFilters())

    def test_from_env_reports_missing_settings(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ProviderError, "AMAZON_CREDENTIAL_ID"):
                CreatorsApiProvider.from_env()

    def test_parse_item_tolerates_missing_fields(self):
        p = parse_item({"asin": "B1"})
        self.assertEqual((p.asin, p.title, p.price), ("B1", "", None))


if __name__ == "__main__":
    unittest.main()
