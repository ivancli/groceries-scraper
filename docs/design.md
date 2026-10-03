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
  redact_headers: [X-CSRF-Token, X-Key]  # in addition to the built-in sensitive headers

session:                       # Session Setup — runs before Start Requests
  setup:
    - request: {url: "https://shop.example/"}
      extract:
        csrf: {css: "meta[name=csrf-token]::attr(content)"}
  refresh_on: [403, 419]
  max_refresh: 2

replay:
  ignore_params: [_ts, csrf]   # excluded from request fingerprint

records:                       # every emitted Record Type; `key:` (Record Key) optional
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
- In a `json` request body, a value that is exactly one `{{ expr }}` keeps the expression's type (`"{{ page }}"` → `2`; `"{{ text | int }}"` for scraped text); anything else renders to a string.

### Session (v1)
- One Session per Site Run; cookies via Scrapy cookie middleware.
- Setup steps run in order before Start Requests; each sees the Session Variables extracted before it. A step's response is read as JSON if its content type says so, else HTML.
- On a status in `refresh_on`: re-run Session Setup, retry the request (its Request Template re-rendered with the new Session Variables), up to `max_refresh` per Run. Requests failing meanwhile wait for the refresh rather than starting another; `refresh_on` statuses are never retried with the stale Session. Past the limit, such requests are dropped and counted (`session/refresh_exhausted`).
- Session Setup failure (non-2xx, network error, or an `extract` with no value) → Run closes with reason `session_setup_failed` → Run Health `failed`.

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
  Use `record_level: "off"` with quotes: YAML treats an unquoted `off` as a boolean.
- `settings.redact_headers` adds case-insensitive header names to redact in request/response
  metadata and Request Templates in the config snapshot. Values become `[REDACTED]`.
  Variables referenced by sensitive header templates are also redacted, as are Variable
  values appearing in sensitive HTTP headers or containing a copy of a sensitive Variable.
  Metadata includes chain Variables, request
  bindings and the Session Variables present when the request was created.
  Scalar copies (including custom values serialized as strings) are compared by their
  string representation, including zero and false; public Variables retain their types.
  Known sensitive values are also masked
  in request/response URL paths, query names/values and fragments after percent-decoding.
  Query values are checked with both literal and form-encoded `+` semantics, including
  unescaped secrets spanning query or URL component delimiters. Original encoding
  outside masked spans is preserved. URL userinfo is always masked.
  The same policy applies to URL-bearing headers (`Location`,
  `Content-Location`, `Referer`); other headers mask copies of known secrets too.
  Redaction builds metadata copies; requests, responses and extraction keep their values.
- `run.json` contains the effective config (including defaults and `--record`), with
  `config_hash` equal to SHA-256 of its UTF-8 JSON with sorted keys and compact separators.
  The hash covers the redacted snapshot; it does not identify changes to secrets.
  The snapshot uses normalized Pipes (`<pipe>` in Fields); unchanged model defaults are
  omitted and restored by the config loader.
- Capture numbers are assigned in response order, including Session Setup, redirects and
  retries. `parent_capture_no` links Follow Requests and successive HTTP attempts. Scrapy's
  internal robots.txt request has no Page Type and is excluded. `errors` mode can leave gaps
  and parents referring to successful Captures that were not kept; `off` writes no Captures
  or Traces, but still writes `run.json` and Records with capture numbers.
- Request `body` is base64 with `body_encoding: base64`, preserving bytes exactly. The `.body`
  file preserves response bytes before HTTP decompression. Header values are lists, preserving
  repeated headers. Response `timing` includes UTC `started_at` / `finished_at` and monotonic
  `elapsed_seconds` for the download.
- Traces contain `capture_no`, `page_type`, the page `loop`, `fields`, `follow` and `dropped`
  entries from the engine. Session Setup uses the `session_setup` filename and traces its
  Variable extraction. Exchanges consumed by redirects/retries have only the Capture link;
  response parsing failures have a top-level `error`.
  Values from custom Steps that JSON cannot encode (such as Decimal) are stored as strings.

## Replay
- Fingerprint = method + canonical URL + body, minus `replay.ignore_params` (query and JSON/form body keys).
- Each Capture stores `request.fingerprint`, computed from the original request before
  metadata redaction: `scrapy-sha1-v1:<40 hex characters>`. It uses Scrapy's default
  fingerprint (headers and URL fragments excluded), after removing the source Run's
  ignored query parameters and top-level JSON/form body keys. With a nonempty ignore
  policy, JSON objects use sorted keys and compact UTF-8 JSON; forms use sorted pairs
  with blank values preserved. Form percent-escaped bytes use the `Content-Type` charset
  (UTF-8 when absent), decoded strictly; unknown charsets or invalid bytes leave the body
  unchanged. Canonical forms use UTF-8. JSON parsing does not use the header's charset
  parameter. Other bodies remain byte-for-byte inputs to the hash.
- Replay must compare this stored identity using the source Run's ignore policy, even
  with an edited extraction config. Redacted URLs are display metadata, never matching
  inputs. Legacy Captures without a fingerprint may be indexed from an original URL;
  a redacted legacy URL cannot safely reconstruct a fingerprint and must be rejected.
- Replay serves responses from a prior Run's Captures; unmatched requests are recorded as `missing`, never fetched.
- Replay produces a **new** Run (optionally with an edited config).

## Run Health
- `ok` / `degraded` / `failed` → exit codes 0 / 1 / 2.
- Built-in `failed`: Session Setup failed, the Run closed early (any close reason but
  `finished` or the `--limit` being reached), or zero Records for any declared Record Type.
- Configured Health Checks breach → `degraded` (v1; per-check severity is a later concern).
- A Run cut short by `--limit` skips the zero-Records and `min_records` checks.
- `run.json` gains `stats` and `health` (`level` plus each breach's `check` and `detail`).
  Stats: duration; pages per Page Type; Records written per Record Type; drops by reason
  without list indices or bad values; null ratio per Record Type and Field path (nested
  Fields only where their object or list element exists); page requests' final outcomes;
  HTTP status histogram over all Captures.
- `max_dropped_ratio` divides drops by drops plus extracted Records (including those the
  limit cut). `max_http_error_ratio` uses page requests' final outcomes: HTTP errors and
  network errors after retries, and refreshes given up; requests robots.txt disallowed
  are not counted. `max_null_ratio` checks each Record Type with that Field. Ratios with an
  empty denominator are 0; `max_*` checks breach only above their threshold.
- `scrape run` ends with a short summary on stdout and exits with the Run Health's code.

## CLI

```
scrape validate sites/<site>.yaml
scrape run sites/<site>.yaml [--limit N] [--record all|errors|off]
scrape replay runs/<site>/<run_id> [--config edited.yaml]
scrape inspect runs/<site>/<run_id> <capture_no> [--field name] [--body]
scrape fixture save runs/<site>/<run_id>          # -> tests/sites/<site>/
```

`inspect` reads the saved Capture and Extraction Trace without a crawl. It shows the HTTP
summary beside Field and Follow Rule Step outputs, labels errors, and prints the parent
chain (marking parents omitted by `record_level: errors`). `--field` limits the trace to
that Field and its nested paths. `--body` pretty-prints the response JSON/HTML, decoding
gzip/deflate first; malformed JSON is shown as text so parsing failures can be inspected.

## Deferred (post-v1)
Playwright (`render: browser`) · Record schema contracts · multiple Sessions per Site · Record Key dedup/diffing (price history) · output sinks (DB/S3) · nightly live smoke runs.
