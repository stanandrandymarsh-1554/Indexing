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

Python 3.9 or newer. The core tool has no third-party dependencies. For **Chrome mode**, the most
reliable way to search amazon.co.uk (see below), also install Playwright once:

```sh
pip3 install playwright
```

Then run it from the project folder:

```sh
python3 -m amazon_search "wireless earbuds"
python3 -m amazon_search --help
```

(`pip install .` also installs an `amazon-search` command, if you prefer.)

### Where results come from

The tool has four backends, chosen with `--backend`:

| Backend | What it does | Needs |
|---|---|---|
| `browser` | Runs the searches in a Chrome window on your computer | `pip3 install playwright`, and Google Chrome |
| `web` | Fetches Amazon's search pages directly, without a browser | Nothing, but Amazon often refuses it |
| `api` | Amazon's official **Creators API** | An Amazon Associates account with at least 10 qualifying sales in the past 30 days |
| `offline` | A small bundled catalog of made-up products | Nothing (for testing) |

The default is `api` if `AMAZON_CREDENTIAL_ID` is set, otherwise `browser` if Playwright is
installed, otherwise `web`. You can also set `AMAZON_SEARCH_BACKEND`.

It searches **amazon.co.uk** by default. Use `--marketplace www.amazon.com` (or set
`AMAZON_MARKETPLACE`) for another Amazon country site.

#### `browser` and `web`: amazon.co.uk directly

Intended for **personal, low-volume use only**. Amazon's Conditions of Use don't allow automated
access, and Amazon blocks clients it thinks are bots. Both modes:

- wait 3 seconds between requests and make at most 12 per search by default (`--max-requests`),
  so a thorough search takes around 30 seconds;
- leave out sponsored results (`--include-sponsored` keeps them);
- stop at once if Amazon shows a robot check or a page they can't read, keeping any results
  found so far. An unreadable page is saved as `amazon_unreadable_page.html` so the parser can be
  fixed.

Amazon increasingly answers plain scripts (`web`) with a "check you're human" page, while serving
normal browsers as usual. `browser` mode avoids that by loading the pages in Chrome. The window
is visible (`--headless` hides it). If Amazon still asks you to confirm you're human, do it in
that window and the search carries on. The tool never tries to solve such checks itself. Chrome
mode uses its own profile, separate from your normal Chrome, kept in
`~/.cache/amazon_search/chrome-profile`, so cookies and passed checks are remembered.

Amazon changes its page layout now and then. If searches suddenly return nothing, the parsing
code in `amazon_search/providers/amazon_web.py` probably needs updating. If you ever share this
tool, switch to the `api` backend or a paid product-data service.

#### `api`: Amazon Creators API

The Creators API replaced the Product Advertising API 5.0, which Amazon retired in May 2026.
Create credentials (a credential ID, a secret and a version such as `3.1`) in Associates Central:

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

The client sends at most one request per second and retries politely when throttled (HTTP 429).

#### `offline`: sample catalog

`--offline` (same as `--backend offline`) searches a small bundled sample catalog
(`amazon_search/data/sample_catalog.json`, made-up products). Like Amazon, it requires every
word to match, and filters narrow the results. That makes it useful for seeing the fallback logic at work. You can point
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
| `--pages` | result pages to fetch for the exact query (default 2) |
| `--quick` | skip related-term searches when the exact query already has results |
| `--min-results N` | keep loosening the search until at least N products are found |
| `--min-score` | hide results that match less than this share of your query (0–1) |
| `--max-requests` | limit on requests to Amazon per search (default 12 for `browser`/`web`, 25 for `api`) |
| `--backend` | `browser`, `web`, `api` or `offline` (see above) |
| `--headless` | Chrome mode: don't show the window |

Filters are treated as preferences. If they rule out everything, they're dropped one at a
time and the output says which ones.

### Speed

A thorough search makes several requests: the exact query, related terms and, if needed,
fallbacks. Use `--quick` to skip related terms when the exact query already has results, or
`--max-requests` to cap the number of requests.

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
    amazon_browser.py Chrome mode: loads search pages in a real browser (Playwright)
    amazon_web.py   Amazon search-page fetcher and parser
    creators_api.py Amazon Creators API client (OAuth2, throttling, retries)
    offline.py      local JSON catalog backend
  data/
    lexicon.json    editable shopping vocabulary
    sample_catalog.json
tests/
```
