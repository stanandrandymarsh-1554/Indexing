# amazon-search

A command-line tool (and Python library) for searching Amazon thoroughly. It:

1. **Searches your exact query**, across several result pages.
2. **Searches related terms**: synonyms (`couch` ↔ `sofa`), more specific product types
   (`headphones` → `earbuds`), expanded abbreviations (`ssd` → `solid state drive`) and
   singular/plural forms. Results from every search are merged and de-duplicated.
3. **Works around empty searches.** If nothing is found, it tries progressively looser searches
   and stops at the first one that returns results:
   1. fix spelling (`wireles headphnes` → `wireless headphones`)
   2. relax your filters one at a time (minimum rating, then price, then category)
   3. try related phrasings without filters
   4. drop the least important words first: sizes and numbers, then colours, then adjectives.
      The main product noun is always kept
      (`purple waterproof bluetooth speaker 100 watt` → `waterproof bluetooth speaker`)
   5. widen to a broader product type (`mechanical keyboard` → `keyboard`)
   6. search the most important words one at a time
4. **Ranks everything by how well it matches what you typed.** Each result is labelled as an
   exact match, a related-term match, or an alternative found by the fallback. The output
   also says which search found it.

## Setup

Python 3.9 or newer. There are no third-party dependencies.

```sh
pip install .            # installs the `amazon-search` command
# or run it without installing:
python -m amazon_search --help
```

### Amazon credentials

Real searches go through Amazon's **Creators API**, the official product-search API for Amazon
Associates. It replaced the Product Advertising API 5.0, which Amazon retired in May 2026. You need:

- an [Amazon Associates](https://affiliate-program.amazon.com/) account, and
- Creators API credentials (a credential ID, a secret and a version such as `3.1`), which you
  create in Associates Central.

```sh
export AMAZON_CREDENTIAL_ID="..."
export AMAZON_CREDENTIAL_SECRET="..."
export AMAZON_CREDENTIAL_VERSION="3.1"        # shown next to your credentials; 2.x/3.x × region
export AMAZON_PARTNER_TAG="yourtag-20"
export AMAZON_MARKETPLACE="www.amazon.com"    # optional, e.g. www.amazon.co.uk
```

| Version | Auth | Region |
|---|---|---|
| 2.1 / 3.1 | Cognito / Login with Amazon | North America |
| 2.2 / 3.2 | Cognito / Login with Amazon | Europe, Middle East, India |
| 2.3 / 3.3 | Cognito / Login with Amazon | Far East |

### Try it without credentials

`--offline` searches a small bundled sample catalog (`amazon_search/data/sample_catalog.json`,
made-up products). It behaves like the real API: every word must match and filters narrow
the results. That makes it useful for seeing the fallback logic at work. You can point
`--catalog` at your own JSON file with the same fields.

```sh
amazon-search --offline "purple waterproof bluetooth speaker 100 watt" -v
```

```
* No results for "purple waterproof bluetooth speaker 100 watt"; trying alternatives.
* dropped: watt, 100, purple
* Showing the closest alternatives:

 1. Portable Bluetooth Speaker, Waterproof, 24H Playtime
    $39.99 · 4.6★ (15,420) · BoomBox Sample · ASIN SAMPLE0006 · match 70% [alternative]
    found via "waterproof bluetooth speaker"
```

## Usage

```sh
amazon-search "noise cancelling headphones"
amazon-search "air fryer" --max-price 60 --min-rating 4 -c HomeAndKitchen
amazon-search "wireles earbuds" --json          # machine-readable output
amazon-search "desk lamp" -v                    # also list every search that was tried
amazon-search                                   # interactive prompt
```

| Option | Meaning |
|---|---|
| `-c/--category` | Amazon search index (`Electronics`, `HomeAndKitchen`, `Fashion`, ...) |
| `--min-price`, `--max-price` | price range in the marketplace currency (e.g. `49.99`) |
| `--min-rating` | 1–4, "at least N stars" |
| `--sort` | `Relevance`, `Price:LowToHigh`, `Price:HighToLow`, `AvgCustomerReviews`, `NewestArrivals`, `Featured` |
| `-n/--limit` | number of results to show (default 20) |
| `--pages` | result pages to fetch for the exact query, 10 items each (default 2) |
| `--quick` | skip related-term searches when the exact query already has results |
| `--min-results N` | keep loosening the search until at least N products are found |
| `--min-score` | hide results that match less than this share of your query (0–1) |
| `--max-requests` | limit on API calls per search (default 25) |

Filters are treated as preferences. If they rule out everything, they're dropped one at a
time and the output says which ones.

### Rate limits

The Creators API limits how many requests per second you can make. The client sends at most
one request per second by default and retries politely when throttled (HTTP 429). A thorough
search makes several API calls, so it can take a few seconds. Use `--max-requests` to limit
the number of calls, or `--quick` for a single fast search when the exact query works.

### Improving term matching

`amazon_search/data/lexicon.json` holds the synonym groups, abbreviations, broader/narrower
product types and the spelling vocabulary. Edit it, or pass your own with `--lexicon`. The tool
also learns words from the product titles it sees and saves them to
`~/.cache/amazon_search/learned_words.json`, so spelling correction improves the more you use
it. Turn this off with `--no-learn`.

## Library use

```python
from amazon_search import SearchEngine, SearchFilters
from amazon_search.providers.creators_api import CreatorsApiProvider

engine = SearchEngine(CreatorsApiProvider.from_env())
report = engine.search("gaming keybord", SearchFilters(max_price=80))
for r in report.results:
    print(r.match_type.value, f"{r.score:.0%}", r.product.title, r.product.url)
print(report.notes)  # e.g. ['No results for ...', 'spelling: "keybord" -> "keyboard"']
```

To add another backend, implement `search(keywords, filters, page) -> list[Product]` and a
`max_page` attribute (see `amazon_search/providers/base.py`).

## Development

```sh
python -m unittest discover -s tests -t .
```

## Project layout

```
amazon_search/
  cli.py            command-line interface
  engine.py         exact search → related terms → fallback ladder, merging and ranking
  expansion.py      query rewriting: synonyms, word forms, spelling, term dropping
  lexicon.py        synonym/abbreviation/broader-term lookups and spelling vocabulary
  relevance.py      scores each product against the original query
  providers/
    creators_api.py Amazon Creators API client (OAuth2, throttling, retries)
    offline.py      local JSON catalog backend
  data/
    lexicon.json    editable shopping vocabulary
    sample_catalog.json
tests/
```
