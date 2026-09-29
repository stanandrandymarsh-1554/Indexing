"""Thorough Amazon product search with related-term expansion and zero-result fallback."""

from .engine import SearchEngine
from .lexicon import Lexicon
from .models import MatchType, Product, ScoredProduct, SearchFilters, SearchReport

__all__ = ["Lexicon", "MatchType", "Product", "ScoredProduct", "SearchEngine", "SearchFilters", "SearchReport"]
__version__ = "0.1.0"
