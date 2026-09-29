"""Client for Amazon's Creators API, the official product-search API for Amazon Associates.

The Creators API replaced the Product Advertising API 5.0 (PA-API), which was retired
in May 2026. You need an Amazon Associates account and Creators API credentials
(credential ID + secret, created in Associates Central) plus your partner/associate tag.

Only the standard library is used so the tool has no install-time dependencies.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

from ..models import Product, SearchFilters
from .base import ProviderError

API_URL = "https://creatorsapi.amazon/catalog/v1"

# The credential "version" Amazon shows next to your credentials selects the token endpoint.
# 2.x credentials authenticate with Amazon Cognito, 3.x with Login with Amazon (LwA).
AUTH_URLS = {
    "2.1": "https://creatorsapi.auth.us-east-1.amazoncognito.com/oauth2/token",  # North America
    "2.2": "https://creatorsapi.auth.eu-south-2.amazoncognito.com/oauth2/token",  # Europe
    "2.3": "https://creatorsapi.auth.us-west-2.amazoncognito.com/oauth2/token",  # Far East
    "3.1": "https://api.amazon.com/auth/o2/token",  # North America
    "3.2": "https://api.amazon.co.uk/auth/o2/token",  # Europe, Middle East, India
    "3.3": "https://api.amazon.co.jp/auth/o2/token",  # Far East
}

SEARCH_RESOURCES = [
    "itemInfo.title",
    "itemInfo.byLineInfo",
    "itemInfo.features",
    "itemInfo.classifications",
    "offersV2.listings.price",
    "customerReviews.count",
    "customerReviews.starRating",
    "images.primary.medium",
]

ITEMS_PER_PAGE = 10  # the API returns at most 10 items per SearchItems call
MAX_PAGE = 10


class CreatorsApiProvider:
    max_page = MAX_PAGE

    def __init__(
        self,
        credential_id: str,
        credential_secret: str,
        partner_tag: str,
        version: str = "3.1",
        marketplace: str = "www.amazon.com",
        min_interval: float = 1.0,
        max_retries: int = 3,
        timeout: float = 20.0,
        opener: Optional[Callable[..., Any]] = None,
    ) -> None:
        if version not in AUTH_URLS:
            raise ValueError(f"Unknown credential version {version!r}; expected one of {sorted(AUTH_URLS)}")
        self.credential_id = credential_id
        self.credential_secret = credential_secret
        self.partner_tag = partner_tag
        self.version = version
        self.marketplace = marketplace
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen
        self._token: Optional[str] = None
        self._token_expires_at = 0.0
        self._last_request_at = 0.0
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, **overrides: Any) -> "CreatorsApiProvider":
        env = os.environ
        missing = [
            name
            for name in ("AMAZON_CREDENTIAL_ID", "AMAZON_CREDENTIAL_SECRET", "AMAZON_PARTNER_TAG")
            if not env.get(name)
        ]
        if missing:
            raise ProviderError(
                "Missing Creators API settings: " + ", ".join(missing) + ". "
                "Set them in your environment (see README), or use --offline to try the sample catalog."
            )
        kwargs: dict[str, Any] = dict(
            credential_id=env["AMAZON_CREDENTIAL_ID"],
            credential_secret=env["AMAZON_CREDENTIAL_SECRET"],
            partner_tag=env["AMAZON_PARTNER_TAG"],
            version=env.get("AMAZON_CREDENTIAL_VERSION", "3.1"),
            marketplace=env.get("AMAZON_MARKETPLACE", "www.amazon.com"),
        )
        kwargs.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**kwargs)

    # ------------------------------------------------------------------ search

    def search(self, keywords: str, filters: SearchFilters, page: int = 1) -> list[Product]:
        body: dict[str, Any] = {
            "partnerTag": self.partner_tag,
            "marketplace": self.marketplace,
            "keywords": keywords,
            "itemCount": ITEMS_PER_PAGE,
            "itemPage": page,
            "resources": SEARCH_RESOURCES,
        }
        if filters.search_index:
            body["searchIndex"] = filters.search_index
        if filters.min_price is not None:
            body["minPrice"] = self._to_lowest_denomination(filters.min_price)
        if filters.max_price is not None:
            body["maxPrice"] = self._to_lowest_denomination(filters.max_price)
        if filters.min_rating is not None:
            body["minReviewsRating"] = filters.min_rating
        if filters.sort_by:
            body["sortBy"] = filters.sort_by

        data = self._call("searchItems", body)
        if _is_no_results(data):
            return []
        items = (data.get("searchResult") or {}).get("items") or []
        return [parse_item(it) for it in items]

    def _to_lowest_denomination(self, amount: float) -> int:
        # Price filters are in the currency's smallest unit: 3241 means $32.41. Yen has no subunit.
        per_unit = 1 if self.marketplace.endswith(".co.jp") else 100
        return max(1, round(amount * per_unit))

    # ------------------------------------------------------------------ HTTP

    def _call(self, operation: str, body: dict[str, Any]) -> dict[str, Any]:
        delay = 2.0
        for attempt in range(self.max_retries + 1):
            self._throttle()
            headers = {
                "Authorization": self._auth_header(),
                "Content-Type": "application/json",
                "x-marketplace": self.marketplace,
            }
            req = urllib.request.Request(
                f"{API_URL}/{operation}", data=json.dumps(body).encode(), headers=headers, method="POST"
            )
            try:
                with self._open(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode() or "{}")
            except urllib.error.HTTPError as e:
                payload = _read_json(e)
                if _is_no_results(payload):
                    return payload
                if e.code == 401 and attempt == 0:
                    self._token = None  # token may have been revoked early; refresh once
                    continue
                if e.code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise ProviderError(f"Creators API {operation} failed (HTTP {e.code}): {_error_text(payload)}") from e
            except urllib.error.URLError as e:
                if attempt < self.max_retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise ProviderError(f"Could not reach the Creators API: {e.reason}") from e
        raise ProviderError(f"Creators API {operation} failed after {self.max_retries + 1} attempts")

    def _throttle(self) -> None:
        with self._lock:
            wait = self._last_request_at + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()

    def _auth_header(self) -> str:
        token = self._access_token()
        if self.version.startswith("3."):
            return f"Bearer {token}"
        return f"Bearer {token}, Version {self.version}"

    def _access_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        url = AUTH_URLS[self.version]
        if self.version.startswith("3."):
            req = urllib.request.Request(
                url,
                data=json.dumps(
                    {
                        "grant_type": "client_credentials",
                        "client_id": self.credential_id,
                        "client_secret": self.credential_secret,
                        "scope": "creatorsapi::default",
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        else:
            basic = base64.b64encode(f"{self.credential_id}:{self.credential_secret}".encode()).decode()
            req = urllib.request.Request(
                url,
                data=urllib.parse.urlencode(
                    {"grant_type": "client_credentials", "scope": "creatorsapi/default"}
                ).encode(),
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                method="POST",
            )
        try:
            with self._open(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise ProviderError(
                f"Authentication failed (HTTP {e.code}): {_error_text(_read_json(e))}. "
                "Check AMAZON_CREDENTIAL_ID / AMAZON_CREDENTIAL_SECRET / AMAZON_CREDENTIAL_VERSION."
            ) from e
        except urllib.error.URLError as e:
            raise ProviderError(f"Could not reach the token endpoint: {e.reason}") from e
        self._token = data["access_token"]
        # refresh 60s early so a request never goes out with a token about to expire
        self._token_expires_at = time.monotonic() + max(60, int(data.get("expires_in", 3600)) - 60)
        return self._token


# ---------------------------------------------------------------------- parsing


def parse_item(item: dict[str, Any]) -> Product:
    info = item.get("itemInfo") or {}
    listing = ((item.get("offersV2") or {}).get("listings") or [{}])[0]
    money = ((listing.get("price") or {}).get("money")) or {}
    reviews = item.get("customerReviews") or {}
    return Product(
        asin=item.get("asin", ""),
        title=_dv(info.get("title")) or "",
        url=item.get("detailPageURL"),
        price=_float(money.get("amount")),
        currency=money.get("currency"),
        display_price=money.get("displayAmount"),
        rating=_float((reviews.get("starRating") or {}).get("value")),
        review_count=_int(reviews.get("count")),
        brand=_dv((info.get("byLineInfo") or {}).get("brand")),
        image_url=(((item.get("images") or {}).get("primary") or {}).get("medium") or {}).get("url"),
        features=list((info.get("features") or {}).get("displayValues") or []),
        category=_dv((info.get("classifications") or {}).get("productGroup")),
    )


def _dv(attr: Any) -> Optional[str]:
    return attr.get("displayValue") if isinstance(attr, dict) else None


def _float(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _read_json(e: urllib.error.HTTPError) -> dict[str, Any]:
    try:
        return json.loads(e.read().decode() or "{}")
    except (ValueError, OSError):
        return {}


def _errors(payload: dict[str, Any]) -> list[dict[str, Any]]:
    errs = payload.get("errors") or payload.get("Errors") or []
    return errs if isinstance(errs, list) else []


def _is_no_results(payload: dict[str, Any]) -> bool:
    return any("noresults" in str(e.get("code", e.get("Code", ""))).lower() for e in _errors(payload))


def _error_text(payload: dict[str, Any]) -> str:
    errs = _errors(payload)
    if errs:
        return "; ".join(f"{e.get('code', e.get('Code', '?'))}: {e.get('message', e.get('Message', ''))}" for e in errs)
    return str(payload.get("message") or payload.get("reason") or payload or "no details")
