"""A local, credential-free provider backed by a JSON product catalog.

It mimics how Amazon's keyword search behaves closely enough to exercise the search
engine: every keyword must match (AND semantics) and filters narrow the result set, so
over-specific or misspelled queries return nothing, just as they can on the real API.
Useful for demos, tests, and developing without an Associates account.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Optional, Union

from ..models import Product, SearchFilters
from ..text import stem, tokenize

PAGE_SIZE = 10


class OfflineProvider:
    max_page = 10

    def __init__(self, products: list[Product]) -> None:
        self.products = products
        self._index = [
            (p, {stem(t) for t in tokenize(" ".join([p.title, p.brand or "", p.category or "", *p.features]))})
            for p in products
        ]

    @classmethod
    def from_file(cls, path: Optional[Union[str, Path]] = None) -> "OfflineProvider":
        if path is None:
            raw = resources.files("amazon_search.data").joinpath("sample_catalog.json").read_text()
        else:
            raw = Path(path).read_text()
        return cls([Product(**p) for p in json.loads(raw)])

    def search(self, keywords: str, filters: SearchFilters, page: int = 1) -> list[Product]:
        terms = {stem(t) for t in tokenize(keywords)}
        if not terms:
            return []
        hits = [p for p, toks in self._index if terms <= toks and _passes(p, filters)]
        hits = _sort(hits, filters.sort_by)
        start = (page - 1) * PAGE_SIZE
        return hits[start : start + PAGE_SIZE]


def _passes(p: Product, f: SearchFilters) -> bool:
    if f.search_index and f.search_index.lower() not in ("all", (p.category or "").lower()):
        return False
    if f.min_price is not None and (p.price is None or p.price < f.min_price):
        return False
    if f.max_price is not None and (p.price is None or p.price > f.max_price):
        return False
    if f.min_rating is not None and (p.rating is None or p.rating < f.min_rating):
        return False
    return True


def _sort(hits: list[Product], sort_by: Optional[str]) -> list[Product]:
    if sort_by == "Price:LowToHigh":
        return sorted(hits, key=lambda p: (p.price is None, p.price or 0))
    if sort_by == "Price:HighToLow":
        return sorted(hits, key=lambda p: (p.price is None, -(p.price or 0)))
    if sort_by == "AvgCustomerReviews":
        return sorted(hits, key=lambda p: -(p.rating or 0))
    return hits
