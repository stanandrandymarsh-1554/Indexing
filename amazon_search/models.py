"""Core data types shared by providers, the search engine and the CLI."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


class MatchType(str, Enum):
    """How a product was found, from most to least faithful to the query."""

    EXACT = "exact"  # returned for the query exactly as typed
    RELATED = "related"  # returned for an expanded query (synonym, variant, ...)
    FALLBACK = "fallback"  # returned only after the query was relaxed/rewritten


@dataclass
class SearchFilters:
    """Optional filters. Fallback logic may drop these if they cause zero results."""

    search_index: Optional[str] = None  # Amazon category, e.g. "Electronics"
    min_price: Optional[float] = None  # in the marketplace currency, e.g. 25.00
    max_price: Optional[float] = None
    min_rating: Optional[int] = None  # 1-4, "at least N stars"
    sort_by: Optional[str] = None  # Relevance, Price:LowToHigh, AvgCustomerReviews, ...

    def is_empty(self) -> bool:
        return all(v is None for v in asdict(self).values())


@dataclass
class Product:
    asin: str
    title: str
    url: Optional[str] = None
    price: Optional[float] = None
    currency: Optional[str] = None
    display_price: Optional[str] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    brand: Optional[str] = None
    image_url: Optional[str] = None
    features: list[str] = field(default_factory=list)
    category: Optional[str] = None


@dataclass
class ScoredProduct:
    product: Product
    score: float  # 0..1 relevance to the ORIGINAL query
    match_type: MatchType
    found_by: str  # the query string that surfaced this product

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self.product)
        d.update(score=round(self.score, 3), match_type=self.match_type.value, found_by=self.found_by)
        return d


@dataclass
class Attempt:
    """One provider call made while searching; kept for transparency/debugging."""

    stage: str
    keywords: str
    filters: SearchFilters
    page: int
    result_count: int
    note: str = ""


@dataclass
class SearchReport:
    query: str
    results: list[ScoredProduct]
    attempts: list[Attempt]
    notes: list[str]  # human-readable explanation of any rewriting that happened

    @property
    def used_fallback(self) -> bool:
        return bool(self.results) and all(r.match_type is MatchType.FALLBACK for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "used_fallback": self.used_fallback,
            "notes": self.notes,
            "results": [r.to_dict() for r in self.results],
            "attempts": [
                {
                    "stage": a.stage,
                    "keywords": a.keywords,
                    "filters": {k: v for k, v in asdict(a.filters).items() if v is not None},
                    "page": a.page,
                    "result_count": a.result_count,
                    "note": a.note,
                }
                for a in self.attempts
            ],
        }
