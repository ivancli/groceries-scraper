# Design — config-driven scraper (v1)

Terminology: see [`CONTEXT.md`](../CONTEXT.md). Architecture: see [ADR-0001](adr/0001-pure-extraction-engine-behind-scrapy-adapter.md).

## Stack

Python 3.12 · `uv` · Scrapy 2.x · Pydantic v2 · `jsonpath-ng` (ext parser) · Jinja2 (`jinja2.sandbox`) · Typer · pytest · ruff · mypy.

## Layout

```
sites/<site>.yaml            # one file per Site
defaults.yaml                # global runtime settings; Site `settings:` overrides
src/groceries_scraper/
  config/                    # Pydantic models, loader, semantic validation
  engine/                    # PURE: pipes, steps, fields, follow rules, trace (no scrapy import)
  adapter/                   # Scrapy spider, capture/replay middleware, pipelines
  run/                       # run directory, summary, health
  cli.py                     # Typer app: validate | run | replay | inspect | fixture
tests/sites/<site>/          # Golden Fixtures (captures + expected records)
runs/<site>/<run_id>/        # Run output (gitignored)
```

## Site config — full example

```yaml
site: example_grocer

settings:                      # allowlisted overrides of defaults.yaml
  download_delay: 1.0
  concurrent_requests_per_domain: 2
  obey_robots: true            # default true; override deliberately
  record_level: all            # all | errors | off

session:                       # Session Setup — runs before Start Requests
  setup:
    - request: {url: "https://shop.example/"}
      extract:
        csrf: {css: "meta[name=csrf-token]::attr(content)"}
  refresh_on: [403, 419]
  max_refresh: 2

replay:
  ignore_params: [_ts, csrf]   # excluded from request fingerprint

records:                       # Record Types + Record Keys
  product: {key: [sku]}

health:
  min_records: {product: 500}
  max_dropped_ratio: 0.05
  max_null_ratio: {price: 0.02}
  max_http_error_ratio: 0.1

start:
  - {url: "https://shop.example/c/dairy", page_type: listing}

page_types:
  listing:
    response: html             # html | json
    items:
      each: {css: "div.tile"}  # page-level Loop (no record: -> navigation only)
    follow:
      - select: {css: "a.tile-link::attr(href)"}
        scope: each            # per Loop node; default: page
        page_type: product_api
        pass:
          sku:   {css: "::attr(data-sku)"}
          price: [{css: ".price::text"}, {regex: '([\d,.]+)'}, {replace: [",", ""]}]
          category: {xpath: "//nav[@class='crumbs']//text()", absolute: true}
        request:               # Request Template (default: GET selected URL)
          method: POST
          url: "https://shop.example/api/product"
          headers: {X-CSRF-Token: "{{ session.csrf }}", X-Key: "{{ env.SHOP_KEY }}"}
          json: {sku: "{{ sku }}"}
      - select: {css: "a.next::attr(href)"}   # pagination
        page_type: listing

  product_api:
    response: json
    record: product            # only Page Types with record: emit Records
    fields:
      sku:      {var: sku, type: string, required: true}
      price:    {var: price, type: number}
      category: {var: category}
      name:     {jsonpath: "$.name", type: string}
      on_sale:  {jsonpath: "$.promo", type: boolean, default: false}
      images:   {type: array, jsonpath: "$.images[*].url", items: {type: string}}
      nutrition:
        type: object
        fields:
          kcal: {jsonpath: "$.nutrition.energy", type: integer}
      variants:
        type: array
        each: {jsonpath: "$.variants[*]"}
        fields:
          size:  {jsonpath: "$.size"}
          price: {jsonpath: "$.price", type: number}
      unit_price: [{jsonpath: "$.cup"}, {fn: "mypkg.transforms:parse_unit_price"}]
```

## Semantics

### Fields
- Types: `string | number | integer | boolean | object | array`.
- `object` → `fields`. `array` → either `items` (scalars: all matches of the Pipe) or `each` + `fields` (Loop over nodes).
- Scalars take the **first** match; arrays take **all**. No `multiple:` flag.
- No match → `null` or `default`. Missing `required: true` → Record dropped, reason in Trace.
- **Coercion** is strict and last: `"1234.50"` → number OK; `"$1,234.50"` → Trace error (clean up in the Pipe).

### Scope
- Every Selector is evaluated relative to its Scope (whole response, or current Loop node).
- CSS/XPath require an HTML Scope; JSONPath requires a JSON Scope. `parse: json|html` switches Scope type mid-Pipe.
- Absolute XPath (`/`, `//`) inside a Loop is a validation error unless the Step sets `absolute: true`.

### Pipe steps (v1)
| Step | Notes |
|---|---|
| `css`, `xpath`, `jsonpath` | Selector steps |
| `parse: json \| html` | switch Scope type |
| `var: name` | read a Variable (`session.x` for Session Variables) |
| `regex: pattern` | group 1 if present, else full match |
| `replace: [old, new]`, `strip`, `split: sep`, `join: sep`, `lower`, `upper` | text |
| `urljoin` | resolve against response URL |
| `template: "{{ ... }}"` | sandboxed Jinja; `value`, Variables, `session`, `env` in scope |
| `fn: "module:callable"` | escape hatch, `(value, ctx) -> value` |

A single step mapping is shorthand for a one-step Pipe. Trace records the value after every step.

### Variables & templating
- Scopes: `session.*` (Session Setup), bare names (passed along the chain by `pass:`), `env.*` (environment; secrets never live in YAML).
- Templates: `jinja2.sandbox.SandboxedEnvironment`, `StrictUndefined`.

### Session (v1)
- One Session per Site Run; cookies via Scrapy cookie middleware.
- On a status in `refresh_on`: re-run Session Setup, retry the request, up to `max_refresh` per Run.
- Session Setup failure → Run Health `failed`.

## Run directory

```
runs/<site>/<run_id>/
  run.json                     # config snapshot + hash, stats, Run Health
  captures/0001-listing.meta.json   # request (redacted), response meta, parent capture, page type, variables
  captures/0001-listing.body        # raw body
  traces/0001-listing.trace.json    # per-field / per-follow-rule step values + errors
  records/product.jsonl        # each record has _meta: site, run_id, record_type, scraped_at, source_url, capture_no
```

- Redacted by default: `Cookie`, `Set-Cookie`, `Authorization`, plus Site-configured headers.
- `record_level: errors` keeps only non-2xx or extraction-error Captures.

## Replay
- Fingerprint = method + canonical URL + body, minus `replay.ignore_params` (query and JSON/form body keys).
- Replay serves responses from a prior Run's Captures; unmatched requests are recorded as `missing`, never fetched.
- Replay produces a **new** Run (optionally with an edited config).

## Run Health
- `ok` / `degraded` / `failed` → exit codes 0 / 1 / 2.
- Built-in `failed`: Session Setup failed, or zero Records for any declared Record Type.
- Configured Health Checks breach → `degraded` (v1; per-check severity is a later concern).

## CLI

```
scrape validate sites/<site>.yaml
scrape run sites/<site>.yaml [--limit N] [--record all|errors|off]
scrape replay runs/<site>/<run_id> [--config edited.yaml]
scrape inspect runs/<site>/<run_id> <capture_no>
scrape fixture save runs/<site>/<run_id>          # -> tests/sites/<site>/
```

## Deferred (post-v1)
Playwright (`render: browser`) · Record schema contracts · multiple Sessions per Site · Record Key dedup/diffing (price history) · output sinks (DB/S3) · nightly live smoke runs.
