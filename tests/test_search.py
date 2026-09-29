import contextlib
import io
import json
import unittest

from amazon_search import Lexicon, MatchType, Product, SearchEngine, SearchFilters
from amazon_search.cli import main
from amazon_search.expansion import QueryPlanner, head_index, weighted_terms
from amazon_search.providers.offline import OfflineProvider
from amazon_search.text import stem, tokenize

LEX = Lexicon.load()


class FakeProvider:
    """Returns canned results for exact keyword strings and records every call."""

    max_page = 10

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def search(self, keywords, filters, page=1):
        self.calls.append((keywords, filters, page))
        return self.responses.get((keywords, page), self.responses.get(keywords, [])) if page == 1 else []


def product(asin, title, **kw):
    return Product(asin=asin, title=title, **kw)


class TextTests(unittest.TestCase):
    def test_tokenize_splits_units_and_hyphens(self):
        self.assertEqual(tokenize("USB-C Cable 16GB & Case"), ["usb", "c", "cable", "16", "gb", "and", "case"])

    def test_stem(self):
        self.assertEqual(stem("batteries"), "battery")
        self.assertEqual(stem("headphones"), "headphone")
        self.assertEqual(stem("boxes"), "box")
        self.assertEqual(stem("glass"), "glass")


class LexiconTests(unittest.TestCase):
    def test_synonyms_are_symmetric(self):
        self.assertIn("sofa", LEX.synonyms("couch"))
        self.assertIn("couch", LEX.synonyms("sofa"))

    def test_spelling_correction(self):
        self.assertEqual(LEX.correct("headphnes"), "headphones")
        self.assertEqual(LEX.correct("wireles"), "wireless")
        self.assertIsNone(LEX.correct("wireless"))

    def test_brand_like_words_are_not_corrected(self):
        self.assertIsNone(LEX.correct("iphone"))

    def test_learning_extends_vocabulary(self):
        lex = Lexicon.load()
        self.assertIsNone(lex.correct("zzfrobnicatr"))
        lex.learn(["Frobnicator Deluxe"])
        self.assertEqual(lex.correct("frobnicatr"), "frobnicator")


class ExpansionTests(unittest.TestCase):
    def setUp(self):
        self.planner = QueryPlanner(LEX)

    def test_head_noun_skips_units_and_detail_clauses(self):
        toks = tokenize("purple bluetooth speaker 100 watt")
        self.assertEqual(toks[head_index(toks)], "speaker")
        toks = tokenize("mechanical keyboard with brown switches")
        self.assertEqual(toks[head_index(toks)], "keyboard")

    def test_weights_rank_specs_below_the_noun(self):
        w = dict(weighted_terms("red wireless mouse 2 pack"))
        self.assertEqual(w["mouse"], 1.0)
        self.assertLess(w["2"], w["red"])
        self.assertLess(w["red"], w["wireless"])

    def test_related_includes_synonyms_and_narrower_terms(self):
        kws = [r.keywords for r in self.planner.related("wireless headphones")]
        self.assertIn("wireless earphones", kws)
        self.assertIn("wireless earbuds", kws)

    def test_fallback_order_spelling_then_filters_then_dropping_words(self):
        filters = SearchFilters(max_price=10, search_index="Electronics")
        steps = list(self.planner.fallback_steps("blue wireles mouse", filters))
        self.assertIn("spelling", steps[0][0].note)
        self.assertEqual(steps[0][0].keywords, "blue wireless mouse")
        self.assertEqual(steps[1][0].filters.max_price, None)
        self.assertEqual(steps[1][0].filters.search_index, "Electronics")
        self.assertEqual(steps[2][0].filters.search_index, None)
        drops = [rw.keywords for step in steps for rw in step if rw.note.startswith("dropped:")]
        self.assertEqual(drops[0], "wireless mouse")  # colour goes before the modifier
        self.assertIn("mouse", drops)  # ... and the head noun is never dropped


class EngineTests(unittest.TestCase):
    def test_exact_results_come_first_and_related_ones_are_merged(self):
        provider = FakeProvider(
            {
                "wireless headphones": [product("A1", "Wireless Headphones Over Ear")],
                "wireless earbuds": [product("A2", "Wireless Earbuds"), product("A1", "Wireless Headphones Over Ear")],
            }
        )
        report = SearchEngine(provider, Lexicon.load()).search("wireless headphones")
        self.assertEqual([r.product.asin for r in report.results], ["A1", "A2"])
        self.assertEqual(report.results[0].match_type, MatchType.EXACT)
        self.assertEqual(report.results[1].match_type, MatchType.RELATED)
        self.assertFalse(report.used_fallback)

    def test_quick_mode_skips_related_terms(self):
        provider = FakeProvider({"wireless headphones": [product("A1", "Wireless Headphones")]})
        SearchEngine(provider, Lexicon.load(), thorough=False).search("wireless headphones")
        self.assertEqual(len(provider.calls), 1)

    def test_zero_results_fall_back_to_spelling_fix(self):
        provider = FakeProvider({"wireless headphones": [product("A1", "Wireless Headphones")]})
        report = SearchEngine(provider, Lexicon.load()).search("wireles headphnes")
        self.assertEqual([r.product.asin for r in report.results], ["A1"])
        self.assertTrue(report.used_fallback)
        self.assertTrue(any("spelling" in n for n in report.notes))
        self.assertGreater(report.results[0].score, 0.9)  # scored against the corrected query

    def test_filters_are_dropped_when_they_block_everything(self):
        class PriceyProvider(FakeProvider):
            def search(self, keywords, filters, page=1):
                self.calls.append((keywords, filters, page))
                if keywords == "air fryer" and filters.max_price is None and page == 1:
                    return [product("F1", "Air Fryer 6 Quart", price=89.99)]
                return []

        report = SearchEngine(PriceyProvider({}), Lexicon.load()).search("air fryer", SearchFilters(max_price=20))
        self.assertEqual(report.results[0].product.asin, "F1")
        self.assertIn("dropped the price filter", report.notes)

    def test_request_budget_is_respected(self):
        provider = FakeProvider({})
        report = SearchEngine(provider, Lexicon.load(), max_requests=4).search("purple waterproof bluetooth speaker")
        self.assertEqual(len(provider.calls), 4)
        self.assertFalse(report.results)
        self.assertTrue(any("Stopped after 4" in n for n in report.notes))

    def test_paging_stops_when_a_page_is_short(self):
        full = [product(f"P{i}", f"Desk Lamp {i}") for i in range(10)]
        provider = FakeProvider({("desk lamp", 1): full})
        SearchEngine(provider, Lexicon.load(), pages=5, thorough=False).search("desk lamp")
        self.assertEqual([c[2] for c in provider.calls], [1, 2])

    def test_empty_query_is_rejected(self):
        with self.assertRaises(ValueError):
            SearchEngine(FakeProvider({}), LEX).search("  !! ")


class OfflineEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.provider = OfflineProvider.from_file()
        self.lex = Lexicon.load()
        self.lex.learn(p.title for p in self.provider.products)
        self.engine = SearchEngine(self.provider, self.lex)

    def test_overly_specific_query_still_finds_the_product(self):
        report = self.engine.search("purple waterproof bluetooth speaker 100 watt")
        self.assertEqual(report.results[0].product.asin, "SAMPLE0006")
        self.assertTrue(report.used_fallback)

    def test_nothing_relevant_returns_empty(self):
        self.assertEqual(self.engine.search("sofa").results, [])

    def test_cli_json_output(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--offline", "--json", "cast iron skilet"])
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["results"][0]["asin"], "SAMPLE0034")


if __name__ == "__main__":
    unittest.main()
