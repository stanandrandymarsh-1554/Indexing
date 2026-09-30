"""Search amazon.com directly by reading its public search-results pages.

Meant for personal, low-volume use. Amazon's Conditions of Use don't allow automated
access, and Amazon rate-limits and blocks clients it thinks are bots. So this client is
deliberately polite: one request every few seconds, a cookie session like a browser's,
and a hard stop, with no retry tricks, as soon as Amazon shows a robot check. If you ever
share or publish this tool, switch to the Creators API or a paid data provider instead.

Amazon changes its page markup from time to time; if results stop appearing, the
selectors in ``SearchPageParser`` are the place to look.
"""

from __future__ import annotations

import gzip
import http.cookiejar
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html.parser import HTMLParser
from typing import Any, Callable, Optional

from ..models import Product, SearchFilters
from .base import ProviderError

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

DEFAULT_MARKETPLACE = "www.amazon.co.uk"

# Search-index names (as used by the Creators API and the --category option) -> the
# department alias Amazon's search URL uses (the `i=` parameter). The aliases differ between
# Amazon sites; unknown names pass through unchanged. If a category filter finds nothing, the
# search engine retries without it, so a wrong alias costs one request, not the search.
DEPARTMENTS_US = {
    "all": "aps",
    "electronics": "electronics",
    "computers": "computers",
    "homeandkitchen": "garden",
    "kitchen": "kitchen",
    "fashion": "fashion",
    "sportsandoutdoors": "sporting",
    "toysandgames": "toys-and-games",
    "petsupplies": "pets",
    "beauty": "beauty",
    "tools": "tools",
    "toolsandhomeimprovement": "tools",
    "books": "stripbooks",
    "videogames": "videogames",
    "automotive": "automotive",
    "office": "office-products",
    "officeproducts": "office-products",
    "grocery": "grocery",
    "baby": "baby-products",
    "health": "hpc",
    "healthpersonalcare": "hpc",
}

DEPARTMENTS_UK = {
    "all": "aps",
    "electronics": "electronics",
    "computers": "computers",
    "homeandkitchen": "kitchen",
    "kitchen": "kitchen",
    "fashion": "fashion",
    "sportsandoutdoors": "sports",
    "toysandgames": "toys",
    "petsupplies": "pets",
    "beauty": "beauty",
    "tools": "diy",
    "toolsandhomeimprovement": "diy",
    "diy": "diy",
    "books": "stripbooks",
    "videogames": "videogames",
    "automotive": "automotive",
    "grocery": "grocery",
    "baby": "baby",
    "health": "drugstore",
    "healthpersonalcare": "drugstore",
}

SORTS = {
    "Relevance": "relevanceblender",
    "Featured": "relevanceblender",
    "Price:LowToHigh": "price-asc-rank",
    "Price:HighToLow": "price-desc-rank",
    "AvgCustomerReviews": "review-rank",
    "NewestArrivals": "date-desc-rank",
}

CURRENCY_SYMBOLS = {"$": "USD", "£": "GBP", "€": "EUR", "¥": "JPY", "₹": "INR", "CA$": "CAD", "A$": "AUD"}

_ROBOT_MARKERS = (
    "validatecaptcha",
    "enter the characters you see below",
    "to discuss automated access to amazon data",
    "sorry, we just need to make sure you're not a robot",
)
_NO_RESULTS_RE = re.compile(r">\s*No results for\s*<", re.I)


class RobotCheckError(ProviderError):
    """Amazon served a CAPTCHA / automated-access page instead of results."""


class UnreadablePageError(ProviderError):
    """Amazon sent a page that is neither results nor "no results" (e.g. a new bot check)."""


#: Where the last page we couldn't read is saved, so it can be inspected or sent for a fix.
DEBUG_PAGE = "amazon_unreadable_page.html"


class AmazonWebProvider:
    max_page = 7  # Amazon rarely serves more than ~7 pages of results for a query

    def __init__(
        self,
        marketplace: str = DEFAULT_MARKETPLACE,
        min_interval: float = 3.0,
        timeout: float = 20.0,
        include_sponsored: bool = False,
        opener: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.host = marketplace.removeprefix("https://").removeprefix("http://").strip("/")
        uk = self.host.endswith(".co.uk")
        self.departments = DEPARTMENTS_UK if uk else DEPARTMENTS_US
        self.headers = dict(HEADERS, **({"Accept-Language": "en-GB,en;q=0.9"} if uk else {}))
        self.min_interval = min_interval
        self.timeout = timeout
        self.include_sponsored = include_sponsored
        if opener is None:
            cookies = urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
            opener = urllib.request.build_opener(cookies).open
        self._open = opener
        self._last_request_at = 0.0
        self._lock = threading.Lock()
        self._blocked = False

    def search_url(self, keywords: str, filters: SearchFilters, page: int = 1) -> str:
        params: dict[str, str] = {"k": keywords}
        if filters.search_index:
            key = re.sub(r"[^a-z]", "", filters.search_index.lower())
            params["i"] = self.departments.get(key, filters.search_index)
        if filters.min_price is not None or filters.max_price is not None:
            lo = "" if filters.min_price is None else str(round(filters.min_price * 100))
            hi = "" if filters.max_price is None else str(round(filters.max_price * 100))
            params["rh"] = f"p_36:{lo}-{hi}"  # price range in pence/cents
        if filters.sort_by and filters.sort_by in SORTS:
            params["s"] = SORTS[filters.sort_by]
        if page > 1:
            params["page"] = str(page)
        return f"https://{self.host}/s?{urllib.parse.urlencode(params)}"

    def search(self, keywords: str, filters: SearchFilters, page: int = 1) -> list[Product]:
        if self._blocked:
            raise RobotCheckError(_blocked_message())
        html = self._fetch(self.search_url(keywords, filters, page))
        low = html.lower()
        if any(m in low for m in _ROBOT_MARKERS):
            self._blocked = True
            raise RobotCheckError(_blocked_message())
        if _NO_RESULTS_RE.search(html):
            # Amazon found nothing for these exact words; anything it shows below that message
            # is its own guess. Report "no results" so our fallback does the guessing, visibly.
            return []
        if page == 1 and "s-search-result" not in html:
            # Not a results page and not "no results" either: most likely a bot check we don't
            # recognise. Stop everything rather than keep firing searches at a page we can't read,
            # which is how a soft check turns into a hard block.
            self._blocked = True
            raise UnreadablePageError(_unreadable_message(_save_debug_page(html)))
        products = parse_search_page(html, self.host, include_sponsored=self.include_sponsored)
        # Amazon's rating/price facets aren't reliable URL parameters, so enforce them here too.
        return [p for p in products if _passes(p, filters)]

    def _fetch(self, url: str) -> str:
        with self._lock:
            wait = self._last_request_at + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()
        req = urllib.request.Request(url, headers=self.headers)
        try:
            with self._open(req, timeout=self.timeout) as resp:
                raw = resp.read()
                encoding = (resp.headers.get("Content-Encoding") or "").lower()
                charset = resp.headers.get_content_charset() or "utf-8"
        except urllib.error.HTTPError as e:
            if e.code in (503, 429):
                self._blocked = True
                raise RobotCheckError(_blocked_message(f"HTTP {e.code}")) from e
            if e.code == 404:
                return ""
            raise ProviderError(f"Amazon returned HTTP {e.code} for {url}") from e
        except urllib.error.URLError as e:
            raise ProviderError(f"Could not reach {self.host}: {e.reason}") from e
        if encoding == "gzip":
            raw = gzip.decompress(raw)
        elif encoding == "deflate":
            raw = zlib.decompress(raw)
        return raw.decode(charset, errors="replace")


def _blocked_message(detail: str = "") -> str:
    extra = f" ({detail})" if detail else ""
    return (
        f"Amazon is blocking automated requests right now{extra}. Wait a while (often an hour or "
        "more) before trying again, and search less often. Opening amazon.com in a normal browser "
        "on the same network and solving any check there can also help."
    )


def _save_debug_page(html: str) -> Optional[str]:
    try:
        with open(DEBUG_PAGE, "w", encoding="utf-8") as f:
            f.write(html)
        return os.path.abspath(DEBUG_PAGE)
    except OSError:
        return None


def _unreadable_message(path: Optional[str]) -> str:
    saved = f" The page was saved to {path}." if path else ""
    return (
        "Amazon sent a page that isn't search results, probably a check for automated "
        f"requests, so the search stopped to avoid getting blocked.{saved} Wait a while "
        "before searching again."
    )


def _passes(p: Product, f: SearchFilters) -> bool:
    if f.min_price is not None and p.price is not None and p.price < f.min_price:
        return False
    if f.max_price is not None and p.price is not None and p.price > f.max_price:
        return False
    if f.min_rating is not None and (p.rating is None or p.rating < f.min_rating):
        return False
    return True


# ---------------------------------------------------------------------- parsing

_VOID = frozenset("area base br col embed hr img input link meta source track wbr".split())


class SearchPageParser(HTMLParser):
    """Pulls products out of an Amazon search-results page.

    Each result is a ``<div data-component-type="s-search-result" data-asin="...">``. Inside
    it: the title in an ``<h2>`` (or its aria-label), the price in
    ``<span class="a-price"><span class="a-offscreen">$12.34</span>``, the star rating in
    ``<span class="a-icon-alt">4.5 out of 5 stars</span>``, the rating count in an
    ``aria-label="1,234 ratings"`` and the image in ``<img class="s-image">``.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict[str, Any]] = []
        self._cur: Optional[dict[str, Any]] = None
        self._depth = 0  # nesting depth inside the current result div
        self._capture: list[tuple[str, int]] = []  # (field, depth at which it started)
        self._price_depth: Optional[int] = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        classes = a.get("class", "").split()
        if self._cur is None:
            if tag == "div" and a.get("data-component-type") == "s-search-result" and a.get("data-asin"):
                self._cur = {"asin": a["data-asin"], "h2": [], "texts": []}
                self._depth = 1
            return

        if tag not in _VOID:
            self._depth += 1
        cur = self._cur
        label = a.get("aria-label", "")
        if tag == "h2":
            cur["h2"].append(label)
            self._capture.append(("h2", self._depth))
        elif tag == "span" and "a-price" in classes and "a-text-price" not in classes and "price" not in cur:
            self._price_depth = self._depth
        elif tag == "span" and "a-offscreen" in classes and self._price_depth is not None and "price" not in cur:
            self._capture.append(("price", self._depth))
        elif tag in ("span", "i") and "a-icon-alt" in classes and "rating" not in cur:
            self._capture.append(("rating", self._depth))
        elif tag == "img" and "s-image" in classes and "image" not in cur:
            cur["image"] = a.get("src")
        if label and "reviews" not in cur:
            m = re.match(r"\s*([\d.,]+\s*[KkMm]?)\s+(?:ratings?|reviews?)\b", label)
            if m:
                cur["reviews"] = m.group(1)
        if tag == "span" and "s-underline-text" in classes and "reviews" not in cur:
            self._capture.append(("reviews_text", self._depth))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        # <img ... /> — handle like a start tag without changing the depth.
        if tag in _VOID:
            self.handle_starttag(tag, attrs)
        else:
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._cur is None or tag in _VOID:
            return
        self._capture = [(f, d) for f, d in self._capture if d < self._depth]
        if self._price_depth is not None and self._depth <= self._price_depth:
            self._price_depth = None
        self._depth -= 1
        if self._depth == 0:
            self.items.append(self._cur)
            self._cur = None
            self._capture = []
            self._price_depth = None

    def handle_data(self, data: str) -> None:
        if self._cur is None:
            return
        text = data.strip()
        if not text:
            return
        cur = self._cur
        cur["texts"].append(text)
        for field, _ in self._capture:
            if field == "h2":
                cur["h2"].append(text)
            elif field not in cur:
                cur[field] = text


def parse_search_page(html: str, host: str = DEFAULT_MARKETPLACE, include_sponsored: bool = False) -> list[Product]:
    parser = SearchPageParser()
    parser.feed(html)
    parser.close()
    out: list[Product] = []
    seen: set[str] = set()
    for it in parser.items:
        asin = it["asin"]
        if asin in seen:
            continue
        sponsored = any(t in ("Sponsored", "Sponsored Sponsored") for t in it["texts"])
        if sponsored and not include_sponsored:
            continue
        title = max((t for t in it["h2"] if t), key=len, default="").strip()
        if not title:
            continue
        seen.add(asin)
        price, currency = _parse_price(it.get("price"))
        out.append(
            Product(
                asin=asin,
                title=re.sub(r"^Sponsored Ad\s*[-–]\s*", "", title),
                url=f"https://{host}/dp/{asin}",
                price=price,
                currency=currency,
                display_price=it.get("price"),
                rating=_parse_rating(it.get("rating")),
                review_count=_parse_count(it.get("reviews") or it.get("reviews_text")),
                image_url=it.get("image"),
            )
        )
    return out


def _parse_price(text: Optional[str]) -> tuple[Optional[float], Optional[str]]:
    if not text:
        return None, None
    m = re.search(r"(\d[\d.,]*)", text)
    if not m:
        return None, None
    num = m.group(1)
    # "1,234.56" (US/UK) vs "1.234,56" (EU): the last separator is the decimal point.
    if "," in num and "." in num:
        num = num.replace(",", "") if num.rfind(".") > num.rfind(",") else num.replace(".", "").replace(",", ".")
    elif "," in num:
        num = num.replace(",", ".") if len(num.split(",")[-1]) == 2 else num.replace(",", "")
    currency = next((c for s, c in sorted(CURRENCY_SYMBOLS.items(), key=lambda x: -len(x[0])) if s in text), None)
    try:
        return float(num), currency
    except ValueError:
        return None, currency


def _parse_rating(text: Optional[str]) -> Optional[float]:
    m = re.match(r"\s*(\d+(?:[.,]\d+)?)", text or "")
    return float(m.group(1).replace(",", ".")) if m else None


def _parse_count(text: Optional[str]) -> Optional[int]:
    m = re.search(r"(\d[\d.,]*)\s*([KkMm]?)", text or "")
    if not m:
        return None
    num, suffix = m.group(1), m.group(2).lower()
    if suffix:
        return int(float(num.replace(",", "")) * (1000 if suffix == "k" else 1_000_000))
    return int(re.sub(r"[.,]", "", num))
