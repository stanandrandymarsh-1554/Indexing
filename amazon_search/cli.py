"""Command-line interface: `amazon-search "wireless earbuds" --max-price 50`."""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .engine import SearchEngine
from .lexicon import Lexicon
from .models import MatchType, SearchFilters, SearchReport
from .providers.base import ProviderError

SORT_CHOICES = ["Relevance", "Featured", "Price:LowToHigh", "Price:HighToLow", "AvgCustomerReviews", "NewestArrivals"]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="amazon-search",
        description="Thorough Amazon product search with related-term expansion and zero-result fallback.",
        epilog="Leave out the query to search interactively.",
    )
    p.add_argument("query", nargs="*", help="what to search for")
    f = p.add_argument_group("filters (dropped automatically if they cause zero results)")
    f.add_argument("-c", "--category", dest="search_index", help='Amazon search index, e.g. "Electronics"')
    f.add_argument("--min-price", type=float)
    f.add_argument("--max-price", type=float)
    f.add_argument("--min-rating", type=int, choices=[1, 2, 3, 4], help="at least N stars")
    f.add_argument("--sort", dest="sort_by", choices=SORT_CHOICES)
    s = p.add_argument_group("search behaviour")
    s.add_argument("-n", "--limit", type=int, default=20, help="results to show (default 20)")
    s.add_argument("--pages", type=int, default=2, help="pages of the exact query to fetch (default 2)")
    s.add_argument("--quick", action="store_true", help="skip related-term expansion when the query has results")
    s.add_argument("--min-results", type=int, default=1, help="keep relaxing the query until this many results")
    s.add_argument("--min-score", type=float, default=0.0, help="hide results below this relevance (0-1)")
    s.add_argument(
        "--max-requests", type=int, help="cap on requests to Amazon per search (default: 12 for web, 25 for api)"
    )
    b = p.add_argument_group("backend")
    b.add_argument(
        "--backend",
        choices=["web", "api", "offline"],
        help="web: read amazon.com search pages directly; api: Amazon Creators API (needs Associates "
        "credentials); offline: bundled sample catalog. Default: api if AMAZON_CREDENTIAL_ID is set, else web",
    )
    b.add_argument("--offline", action="store_true", help="same as --backend offline")
    b.add_argument("--include-sponsored", action="store_true", help="web backend: keep sponsored (ad) results")
    b.add_argument("--catalog", metavar="FILE.json", help="catalog for --offline (default: bundled sample catalog)")
    b.add_argument(
        "--marketplace",
        help="Amazon site, e.g. www.amazon.com (default: $AMAZON_MARKETPLACE, else www.amazon.co.uk "
        "for web and www.amazon.com for api)",
    )
    b.add_argument("--lexicon", help="path to a custom lexicon.json (synonyms, abbreviations, ...)")
    b.add_argument("--no-learn", action="store_true", help="don't remember words seen in results between runs")
    o = p.add_argument_group("output")
    o.add_argument("--json", action="store_true", help="print machine-readable JSON")
    o.add_argument("-v", "--verbose", action="store_true", help="show every query that was tried")
    return p


def choose_backend(args: argparse.Namespace) -> str:
    if args.backend:
        return args.backend
    if args.offline or args.catalog:
        return "offline"
    env = os.environ.get("AMAZON_SEARCH_BACKEND")
    if env in ("web", "api", "offline"):
        return env
    return "api" if os.environ.get("AMAZON_CREDENTIAL_ID") else "web"


def make_provider(args: argparse.Namespace):
    backend = choose_backend(args)
    if backend == "offline":
        from .providers.offline import OfflineProvider

        return OfflineProvider.from_file(args.catalog)
    if backend == "web":
        from .providers.amazon_web import DEFAULT_MARKETPLACE, AmazonWebProvider

        return AmazonWebProvider(
            marketplace=args.marketplace or os.environ.get("AMAZON_MARKETPLACE", DEFAULT_MARKETPLACE),
            include_sponsored=args.include_sponsored,
        )
    from .providers.creators_api import CreatorsApiProvider

    return CreatorsApiProvider.from_env(marketplace=args.marketplace)


def learned_words_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "amazon_search" / "learned_words.json"


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        provider = make_provider(args)
    except (ProviderError, ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    lexicon = Lexicon.load(args.lexicon)
    backend = choose_backend(args)
    offline = backend == "offline"
    learn_path = None if (args.no_learn or offline) else learned_words_path()
    if learn_path:
        lexicon.load_learned(learn_path)
    if offline:
        lexicon.learn(p.title for p in provider.products)

    engine = SearchEngine(
        provider,
        lexicon,
        pages=args.pages,
        thorough=not args.quick,
        max_requests=args.max_requests or (12 if backend == "web" else 25),
        min_results=args.min_results,
        min_score=args.min_score,
    )
    filters = SearchFilters(
        search_index=args.search_index,
        min_price=args.min_price,
        max_price=args.max_price,
        min_rating=args.min_rating,
        sort_by=args.sort_by,
    )

    queries = [" ".join(args.query)] if args.query else None
    status = 0
    try:
        for query in queries or _prompt():
            try:
                report = engine.search(query, filters, limit=args.limit)
            except ValueError as e:
                print(f"error: {e}", file=sys.stderr)
                status = 2
                continue
            if args.json:
                print(json.dumps(report.to_dict(), indent=2))
            else:
                print_report(report, verbose=args.verbose)
            if queries and not report.results:
                status = 1
    except ProviderError as e:
        print(f"error: {e}", file=sys.stderr)
        status = 2
    except KeyboardInterrupt:
        status = 130
    finally:
        if learn_path:
            try:
                lexicon.save_learned(learn_path)
            except OSError:
                pass
    return status


def _prompt():
    while True:
        try:
            q = input("\nsearch> ").strip()
        except EOFError:
            print()
            return
        if q in ("", "q", "quit", "exit"):
            if q:
                return
            continue
        yield q


LABEL = {MatchType.EXACT: "", MatchType.RELATED: " [related]", MatchType.FALLBACK: " [alternative]"}


def print_report(report: SearchReport, verbose: bool = False) -> None:
    width = 100
    for note in report.notes:
        print(f"* {note}")
    if report.used_fallback:
        print("* Showing the closest alternatives:")
    if report.notes or report.used_fallback:
        print()

    for i, r in enumerate(report.results, 1):
        p = r.product
        head = f"{i:>2}. {p.title}"
        print(textwrap.shorten(head, width=width, placeholder="..."))
        bits = [p.display_price or (f"{p.price:.2f} {p.currency or ''}".strip() if p.price is not None else "price n/a")]
        if p.rating is not None:
            bits.append(f"{p.rating:.1f}★" + (f" ({p.review_count:,})" if p.review_count else ""))
        if p.brand:
            bits.append(p.brand)
        bits.append(f"ASIN {p.asin}")
        bits.append(f"match {r.score:.0%}{LABEL[r.match_type]}")
        print("    " + " · ".join(bits))
        if r.match_type is not MatchType.EXACT:
            print(f'    found via "{r.found_by}"')
        if p.url:
            print(f"    {p.url}")

    if not report.results:
        print(f'No products found for "{report.query}".')

    if verbose:
        print(f"\nQueries tried ({len(report.attempts)}):")
        for a in report.attempts:
            filt = ", ".join(f"{k}={v}" for k, v in asdict_nonnull(a.filters).items())
            extra = f" [{filt}]" if filt else ""
            note = f"  ({a.note})" if a.note else ""
            print(f"  {a.stage:<8} p{a.page}  {a.result_count:>3} hits  {a.keywords!r}{extra}{note}")


def asdict_nonnull(obj) -> dict:
    return {k: v for k, v in asdict(obj).items() if v is not None}


if __name__ == "__main__":
    sys.exit(main())
