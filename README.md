# Groceries Scraper

Scrape websites using YAML configuration: define entry URLs, extraction selectors,
and links or API requests to follow. The generic Scrapy spider writes JSONL Records
and saves HTTP Captures for inspection and offline Replay.

## Set up

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then clone the
repository and install its locked dependencies:

```bash
git clone https://github.com/ivancli/groceries-scraper.git
cd groceries-scraper
uv sync --locked
uv run playwright install chromium  # for `render: browser` Page Types and the test suite
```

The project uses Python 3.12 (`.python-version`); uv can install it if needed.
Run the commands below from the repository root. Global settings are loaded from
`defaults.yaml` in the checkout, and output paths are relative to the working directory.

## Try the included retailer

```bash
uv run scrape validate sites/aldi.yaml
uv run scrape run sites/aldi.yaml --limit 5 --record all
```

[`sites/aldi.yaml`](sites/aldi.yaml) extracts name, brand, price, currency,
size, unit-price text, and product URL from ALDI Australia's public product catalogue.
It requires no account, postcode, store selection, or custom Python function.
It starts at `/products` and follows the next-page links through the catalogue.
The same extraction rules work across product categories. To focus a Run on a
category, replace the Start Request URL with that category's listing URL; its
pagination uses the same rule. Product detail pages are not fetched.
Prices reflect that public listing rather than a selected store's inventory.

`--limit` caps emitted Records, not requests or downloaded pages. A page containing
30 products can still be downloaded in full when the Record limit is five.

The CLI prints the new Run directory and a summary. Find the products at:

```text
runs/aldi/<run_id>/records/product.jsonl
```

Each line is one JSON object. Its `_meta` gives the Site, Run id, Location, Record Type,
scrape time, source URL, and Capture number. The Run also contains:

```text
run.json       effective config, statistics, finish reason, and Run Health
captures/      HTTP request/response metadata and raw response bodies
traces/        selector results, transformation steps, and extraction errors
records/       one JSONL file per Record Type
```

Live websites can change independently of this repository. If the example stops
producing Records, use Captures and Traces to update its selectors.

## Configure another Site

1. Inspect the response HTML or JSON for the page/API you want to scrape. Browser
   developer tools' Network panel helps identify JSON endpoints and request bodies.
   Selectors must match the downloaded response, which can differ from the browser DOM.
   If a page only shows its content after scripts run and no JSON API serves it, set the
   Page Type's `render: browser` to extract from the DOM rendered by headless Chromium.
2. Create `sites/<site>.yaml`. Use a simple Site name containing letters, numbers,
   underscores, or hyphens so it can also be saved as a Golden Fixture.
3. Define `start` URLs and their `page_type`. Start Requests are GET requests.
4. Define each Page Type's `response` (`html` or `json`), optional `items.each` Loop,
   `record`, and `fields`. Only Page Types declaring `record` emit Records.
5. Add Follow Rules for pagination or listing-to-detail requests. Use `pass` to
   carry values to a child Page Type, and Request Templates for POSTs or headers.
6. Add Session Setup if you need cookies or tokens. Read credentials from environment
   variables using `{{ env.NAME }}`; don't put credentials into YAML. If a store or
   postcode changes the data, declare `locations` and pick one per Run with
   `--location`; `session.pool` spreads a Run across several Sessions.
7. Validate, then run a small trial and inspect its output before expanding the crawl.

For example, adapt these URLs and selectors to a real HTML listing:

```yaml
site: my_shop
records:
  product: {key: [name]}
health:
  min_records: {product: 1}
  max_dropped_ratio: 0
  max_http_error_ratio: 0
start:
  - {url: "https://shop.example/products", page_type: listing}
page_types:
  listing:
    record: product
    items: {each: {css: ".product"}}
    fields:
      name: {css: ".name::text", type: string, required: true}
      price: {css: ".price::attr(data-price)", type: number, required: true}
    follow:
      - select: {css: "a.next::attr(href)"}
        page_type: listing
```

The example above is illustrative; `shop.example` is a placeholder. The included
ALDI config is the runnable example. Use a stable product identifier as the Record
Key when available: within a Run, a Record repeating an earlier Record's key, or with
a null key Field, is dropped (and counts toward `max_dropped_ratio`).

Numeric conversion is strict: `3.50` works, but `$3.50` needs cleaning with an XPath
expression or Pipe before conversion. A missing required Field drops the Record;
an optional Field becomes `null` or its configured default. Inside a Loop use relative
XPath (`.//...`); absolute XPath requires `absolute: true`.

Site `settings` override [`defaults.yaml`](defaults.yaml). Defaults include a one-second
download delay, two concurrent requests per domain, obeying `robots.txt`, and recording
all Captures. Write `record_level: "off"` with quotes because YAML interprets bare `off`
as a boolean.

See the [full configuration example and semantics](docs/design.md#site-config--full-example)
for nested Fields, Pipes, JSON APIs, sessions, and health thresholds. That example
uses placeholder URLs and an illustrative `SHOP_KEY` environment variable.

## Inspect and iterate offline

Replace `<run_id>` with the Run directory printed by the CLI and use a Capture
number from `captures/` (Session Setup can be Capture 1).

```bash
uv run scrape inspect runs/aldi/<run_id> 1 --body
uv run scrape inspect runs/aldi/<run_id> 1 --field price
uv run scrape replay runs/aldi/<run_id> --config sites/aldi.yaml
```

Replay serves saved responses, creates a new Run, and never fetches unmatched requests.
It requires a source Run recorded with `record_level: all`. Changing selectors works
offline; adding URLs or changing request bodies can produce missing Captures.

## Compare Runs

`diff` matches two Runs' Records by Record Key and lists those added, removed or
changed, with each changed Field's old and new value:

```bash
uv run scrape diff runs/aldi/<old_run_id> runs/aldi/<new_run_id> --field price
uv run scrape diff runs/aldi/<old_run_id> runs/aldi/<new_run_id> --json
```

Run and Replay exit with `0` for healthy, `1` for degraded, and `2` for failed Run
Health. Validation errors exit nonzero too. Check `run.json` for the reasons.

## Export to Postgres or S3

Install the extra for each backend, then pass `--sink` to `run`/`replay`, or export a
saved Run later. Failed Runs are skipped unless `--force`; re-exporting replaces the
earlier copy. Sinks are checked before the crawl; a Sink failure exits `3`.

```bash
uv sync --locked --extra postgres --extra s3
uv run scrape run sites/aldi.yaml --sink "postgres://user:pass@host/db"
uv run scrape export runs/aldi/<run_id> --sink s3://my-bucket/scrapes
```

S3 credentials and region come from the standard AWS environment. See
[design](docs/design.md#sinks) for the table and object layout.

## Archive a Run to S3

`--archive` copies the whole Run directory (Captures and Extraction Traces included) to S3 after every
Run, failed ones too, so it can be inspected after the machine is gone. It needs the `s3`
extra, must not share a prefix with an S3 `--sink`, and exits `4` if it is unusable or
fails (a Sink failure's `3` wins).

```bash
uv run scrape run sites/aldi.yaml --archive s3://my-bucket/archive --sink s3://my-bucket/scrapes
```

## Save and test Golden Fixtures

Finish a crawl without `--limit`, with recording enabled, then save it:

```bash
uv run scrape run sites/aldi.yaml --record all
uv run scrape fixture save runs/aldi/<run_id>
uv run pytest -q tests/test_sites.py
```

A full ALDI Run visits all catalogue pages and can take several minutes at the
default download delay. Saving requires a completed healthy Run with Records and Captures. Limited Runs are
rejected because Replay could emit Records the original limit discarded. Older Runs
without a saved finish reason need to be captured again.

The fixture lives at `tests/sites/<site>/`, containing `run.json`, Captures, and
expected `records/*.jsonl` with volatile `_meta` fields (`run_id`, `scraped_at`,
`capture_no`) removed. Stable metadata such as Site, Record Type, and source URL
is retained and compared. Saving again replaces that Site's
Captures, manifest and expected Records, preserving unrelated files such as fixture
notes. Capture bodies are preserved exactly; inspect them
before committing fixtures from authenticated Sites because raw bodies can contain secrets.

Tests discover every saved Site fixture, replay it against the **current**
`sites/<site>.yaml`, and compare Records. Record order and volatile metadata are ignored;
duplicates and nested array order are preserved. Changed extraction produces a readable
diff. Missing Captures, unhealthy Replay, and attempted network connections fail the test.
These tests run in the existing CI pytest step, without contacting retailers.

## Development and current limits

```bash
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest
```

Postgres Sink tests skip unless `TEST_DATABASE_URL` is set. Start a disposable database
with Docker Compose and point the tests at it:

```bash
docker compose up -d --wait
TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres uv run pytest
docker compose down
```

A proxy per Session, price history across many Runs, and scheduled live checks are deferred.
Use HTML or JSON endpoints for now. Offline fixtures detect changes to extraction
code and configs; they do not detect changes to the live website.

Architecture and terminology: [design](docs/design.md), [domain model](CONTEXT.md),
and [ADR-0001](docs/adr/0001-pure-extraction-engine-behind-scrapy-adapter.md).
