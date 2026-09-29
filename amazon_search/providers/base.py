from __future__ import annotations

from typing import Protocol

from ..models import Product, SearchFilters


class ProviderError(RuntimeError):
    """Raised for errors that retrying with a different query will not fix (auth, quota, network)."""


class SearchProvider(Protocol):
    """Anything that can run a single keyword search and return one page of products."""

    #: Largest page number the backend will serve.
    max_page: int

    def search(self, keywords: str, filters: SearchFilters, page: int = 1) -> list[Product]:
        """Return products for one page. An empty list means "no results", not an error."""
        ...
