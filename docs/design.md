# Design — config-driven scraper (v1)

Terminology: see [`CONTEXT.md`](../CONTEXT.md). Architecture: see [ADR-0001](adr/0001-pure-extraction-engine-behind-scrapy-adapter.md) and [ADR-0002](adr/0002-location-per-run-session-pool-within-run.md).

## Stack

Python 3.12 · `uv` · Scrapy 2.x · scrapy-playwright (`render: browser`) · Pydantic v2 · `jsonpath-ng` (ext parser) · Jinja2 (`jinja2.sandbox`) · Typer · pytest · ruff · mypy.

## Layout

```
sites/<site>.yaml            # one file per Site
defaults.yaml                # global runtime settings; Site `settings:` overrides
src/groceries_scraper/
  config/                    # Pydantic models, loader, semantic validation
  engine/                    # PURE: pipes, steps, fields, follow rules, trace (no scrapy import)
  adapter/                   # Scrapy spider, capture/replay middleware, pipelines
  run/                       # run directory, summary, health, diff, sinks, supply outcomes
  cli.py                     # Typer app: validate | run | replay | inspect | fixture | diff
tests/sites/<site>/          # Golden Fixtures (captures + expected records)
runs/<site>/<run_id>/        # Run output (gitignored)
Dockerfile                   # one image for every Site, published to GHCR by CI
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

locations:                     # optional; each Run scrapes one (`--location`)
  melb_cbd: {postcode: "3000"}
  sydney_cbd: {postcode: "2000"}

session:                       # Session Setup — runs before Start Requests
  pool: 1                      # interchangeable Sessions per Run (default 1)
  setup:
    - request: {url: "https://shop.example/"}
      extract:
        csrf: {css: "meta[name=csrf-token]::attr(content)"}
    - request: {method: POST, url: "https://shop.example/api/set-postcode", json: {postcode: "{{ location.postcode }}"}}
  refresh_on: [403, 419]
  max_refresh: 2

replay:
  ignore_params: [_ts, csrf]   # excluded from request fingerprint

records:                       # every emitted Record Type; `key:` (Record Key) optional, dedupes
  product: {key: [sku]}        # optional `fields:` = Record Contract (see Record Contracts)

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
    render: http               # http | browser (rendered DOM; see Browser rendering)
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
      unit_price: [{jsonpath: "$.cup"}, {regex: '([\d,.]+)'}, {replace: [",", ""]}]
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
| `var: name` | read a Variable (`session.x` for Session Variables, `location.x` for Location ones) |
| `regex: pattern` | group 1 if present, else full match |
| `replace: [old, new]`, `strip`, `split: sep`, `join: sep`, `lower`, `upper` | text |
| `urljoin` | resolve against response URL |
| `template: "{{ ... }}"` | sandboxed Jinja; `value`, Variables, `session`, `location`, `env` in scope |
| `fn: "module:callable"` | escape hatch, `(value, ctx) -> value` |

A single step mapping is shorthand for a one-step Pipe. Trace records the value after every step.

### Variables & templating
- Scopes: `session.*` (Session Setup), `location.*` (the Run's Location), bare names (passed along the chain by `pass:`), `env.*` (environment; secrets never live in YAML).
- Templates: `jinja2.sandbox.SandboxedEnvironment`, `StrictUndefined`.
- In a `json` request body, a value that is exactly one `{{ expr }}` keeps the expression's type (`"{{ page }}"` → `2`; `"{{ text | int }}"` for scraped text); anything else renders to a string.

### Browser rendering
- `render: browser` loads a Page Type's requests in headless Chromium via scrapy-playwright and
  extracts from the rendered DOM, captured once the network is idle. It needs `response: html`;
  prefer a JSON API where the site has one.
- Only Page Types opt in: Session Setup, robots.txt and other Page Types stay plain HTTP, and
  a Site without browser Page Types never starts a browser.
- The browser sends the request's method, headers (including Session cookies) and body for the
  page itself. Its subresources and script fetches use the browser's own headers and cookie
  store, so they lack Session cookies and headers, and skip robots.txt, download delay and
  Captures. Redirects happen inside the browser, so the Capture is the final page.
- Navigation timeouts are retried like HTTP download timeouts.
- Its Capture has `render: browser`; request headers are those the browser sent, and the
  `.body` is the rendered DOM (no `Content-Encoding`), not the server's bytes. Replay serves
  it like any Capture, without a browser.
- Setup: `uv run playwright install chromium` (CI installs it too).

### Locations
- `locations:` maps a name (letters, digits, `_`, `-`) to that Location's Variables, read as
  `location.*` (fixed for the Run). A Site without it has one implicit Location, `default`,
  with no Variables. Validation requires every `location.*` reference to be set by every
  declared Location.
- `scrape run --location <name>` picks the Run's Location; it is required when the Site
  declares any, and an unknown name is an error listing the declared ones. Running every
  Location means one Run each, left to the scheduler (ADR-0002).
- The Location is stored in `run.json`, every Record's `_meta.location`, the Postgres
  `scrape_runs.location` column and S3 paths. Runs from before Locations read as `default`.
  Replay reuses the source Run's Location; `diff` rejects Runs of different Locations.

### Sessions
- `session.pool` Sessions per Run (default 1), each with its own cookie jar (Scrapy's
  `cookiejar` meta key) and Session Variables. A Session never changes what data is returned.
- Start Requests are assigned to Sessions round-robin in config order, so Replay assigns
  them identically; every request followed from a Start Request uses its Session. A pool
  larger than the Start Requests is warned about: the extra Sessions stay idle.
- Setup steps run in order, once per Session, before its Start Requests; each sees the
  Session Variables extracted before it. A step's response is read as JSON if its content
  type says so, else HTML.
- On a status in `refresh_on`: re-run that Session's Setup, retry the request (its Request
  Template re-rendered with the new Session Variables), up to `max_refresh` per Session.
  Requests failing meanwhile wait for the refresh rather than starting another;
  `refresh_on` statuses are never retried with the stale Session. Past the limit, such
  requests are dropped and counted (`session/refresh_exhausted`).
- A Session is lost when its Setup fails (non-2xx, network error, or an `extract` with no
  value) or its refresh budget runs out. Its Start Requests not yet sent move to the
  remaining Sessions; requests waiting for its refresh are dropped (`Session lost`). The
  Run is at best `degraded` (`session/lost`); requests already sent with it run on, and
  any later refused ones are dropped (`Session lost`). Losing every Session closes the Run
  with reason `session_setup_failed` → Run Health `failed` — with the default single
  Session, so does running out of `max_refresh`.
- `download_delay` and `concurrent_requests_per_domain` stay per Site: the pool spreads
  cookie identities, not request rate.

### Supplied Start Requests
- A Site may declare an Accepts Rule, so others can hand it product URLs to check
  ([ADR-0005](adr/0005-dispatcher-schedules-supplied-start-requests.md)):

  ```yaml
  accepts:
    page_type: product                          # must have no Follow Rules
    url: '^https://shop\.example/product/'      # Python/JavaScript-shared regex subset
    examples: [https://shop.example/product/milk-2l]
  ```

- The tracker matches the same pattern in JavaScript, so validation requires a leading `^`
  and allows only `(?:…)`, `(?=…)` and `(?!…)` groups: no look-behind, named groups,
  inline flags, `\A`/`\Z`, `{,n}` or possessive quantifiers. Python matches with ASCII-only
  `\d`/`\w`, as JavaScript does. Every example must match. The accepted Page Type must not
  follow links, so a supplied URL never turns into a crawl; it is reachable like a Start
  Request's Page Type, and gets no Variables.
- `scrape run --supply FILE` reads JSONL `{"ref": …, "url": …}` and requests those URLs
  instead of `start:`, each at the accepted Page Type. Refs and URLs must be unique, and
  URLs free of whitespace (Python's `$` matches before a trailing newline). A URL the
  pattern doesn't match, a malformed line, a Site without `accepts` or `--limit` is refused
  before any request (exit 1, no Run directory).
- Every Record carries `_meta.ref`. Its `scraped_at` is when its Capture was fetched, so
  a Replay keeps it.
- `outcomes.jsonl` holds one Start Request Outcome per ref, in supply order:
  `{"ref", "url", "outcome"}`, plus `error` for `failed`.

  | Outcome | When |
  |---|---|
  | `ok` | at least one Record written |
  | `not_found` | final response 404/410 |
  | `blocked` | final response 403/429, including a `refresh_on` status past `max_refresh` |
  | `skipped` | denied by robots.txt (or missing from a Replay's Captures), or never ended before the Run did |
  | `failed` | anything else: other HTTP errors, network errors, extraction errors, no Record, Session lost |

  Detecting a challenge page as `blocked` needs a per-page Health Check, which doesn't
  exist yet; such a page is `failed` when it yields no Record.

### Record Keys
- Within a Run, a Record whose Record Key matches an earlier Record of its Record Type is
  dropped (`duplicate Record Key`), as is one with a null or absent key Field. Drops are
  recorded in the Extraction Trace and stats like missing required Fields, so they count
  toward `max_dropped_ratio`. Records the `--limit` later cuts still claim their key.
  With `record_level: errors`, a duplicate alone does not keep its Capture: listings
  routinely repeat products.
- Key values compare as canonical JSON, so structured values match whatever their key order.

### Record Contracts
- A Record Type may declare `fields:` once at Site level; `scrape validate` (and every Run)
  then checks each reachable Page Type emitting it against that contract. It is static:
  emitted Records are not re-checked.
- The contract is closed, types are exact, and required is at least as strict: every contract
  Field must exist with the same explicit `type` (an untyped Field skips Coercion, so nothing
  would enforce it), and no other Field may; a contract `required: true` needs
  `required: true` on the Page Type (`default` is not enough: a failed Coercion still yields
  null), while an emitter may require a Field the contract leaves optional.
- Unreachable emitting Page Types are not checked (they never run); the unreachable warning
  flags them, and they are checked once reachable.
- Nested Fields are checked too. An `object` takes `fields`; an `array` takes either
  `items` (scalar type, matching a Page Type's `items`) or `fields` (matching `each` + `fields`).

```yaml
records:
  product:
    key: [sku]
    fields:
      sku:    {type: string, required: true}
      price:  {type: number}
      images: {type: array, items: {type: string}}
      variants: {type: array, fields: {size: {type: string}}}
```

## Run directory

```
runs/<site>/<run_id>/
  run.json                     # Location, Kubernetes Job, config snapshot + hash, stats, Run Health
  captures/0001-listing.meta.json   # request (redacted), response meta, parent capture, page type, variables
  captures/0001-listing.body        # raw body
  traces/0001-listing.trace.json    # per-field / per-follow-rule step values + errors
  records/product.jsonl        # each record has _meta: site, run_id, location, record_type, scraped_at, source_url, capture_no (+ ref when supplied)
  supply.jsonl                 # supplied Runs only: the Supplied Start Requests, for Replay
  outcomes.jsonl               # supplied Runs only: one Start Request Outcome per ref
```

- A supplied Run's `run.json` has `supplied: true` and `supplied_refs` (the ref count).

- `--archive` copies the finished directory to S3 as is (see [Archive](#archive)).
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
- Inside a Kubernetes Job, `run.json` also records `job`: the Job's name, from the
  `SCRAPE_JOB_NAME` environment variable (set through the Downward API), so a Run
  directory or Archive traces back to the Job and its logs. Without the variable there is
  no `job` key; readers treat it as optional. A Replay records its own Job, not its
  source Run's.
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
  Variable extraction; with a pool of several Sessions, Setup Captures carry `session_no`.
  Exchanges consumed by redirects/retries have only the Capture link;
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
  Requests sharing a fingerprint (retries, Session refreshes) get its Captures in Capture
  order, then the last one again; Session Setup requests only those of their own Session.
  Served responses use the request's URL and drop
  `[REDACTED]` header values. robots.txt is not consulted and there is no download delay.
- The source Run must have `record_level: all`. Without `--config`, its config snapshot is
  replayed (redacted header templates send `[REDACTED]`; headers never affect matching).
- Replay produces a **new** Run (optionally with an edited config) of the source Run's
  Location, which an edited config must still declare. Its `run.json` adds
  `replay_of` (`site`, `run_id`) and `missing` (redacted request metadata, as in Captures);
  `stats.requests.missing` counts them. Missing requests are not HTTP errors and do not
  affect Run Health. A redirect whose `Location` was partly redacted cannot be followed.
- A Replay of a supplied Run supplies the same Start Requests (an edited config must still
  accept them) and writes its own `outcomes.jsonl`. robots.txt-denied refs have no
  Capture, so they are `missing` and stay `skipped`.
- An edited config's `replay.ignore_params` applies to the new Run's Captures only; a
  difference from the source Run's is warned about.

## Run Health
- `ok` / `degraded` / `failed` → exit codes 0 / 1 / 2.
- Built-in `failed`: every Session was lost, the Run closed early (any close reason but
  `finished` or the `--limit` being reached), or zero Records for any declared Record Type.
- Configured Health Checks breach → `degraded` (v1; per-check severity is a later concern),
  as does losing some of the Run's Sessions (`session/lost`).
- A Run cut short by `--limit` skips the zero-Records and `min_records` checks.
- `run.json` gains `stats` and `health` (`level` plus each breach's `check` and `detail`).
  Stats: duration and finish reason; pages per Page Type; Records written per Record Type;
  drops by reason without list indices or bad values; null ratio per Record Type and Field path (nested
  Fields only where their object or list element exists); page requests' final outcomes;
  HTTP status histogram over all Captures; Session Pool size and Sessions lost.
- `max_dropped_ratio` divides drops by drops plus extracted Records (including those the
  limit cut). `max_http_error_ratio` uses page requests' final outcomes: HTTP errors and
  network errors after retries, and refreshes given up; requests robots.txt disallowed
  are not counted. `max_null_ratio` checks each Record Type with that Field. Ratios with an
  empty denominator are 0; `max_*` checks breach only above their threshold.
- `scrape run` ends with a short summary on stdout and exits with the Run Health's code,
  `4` when the `--archive` is unusable or fails to take the Run, or `3` (over `4`) when a
  `--sink` is unusable or fails to take a non-failed Run.

## CLI

For installation and a runnable retailer example, see the [quickstart](../README.md).

```
scrape validate sites/<site>.yaml
scrape run sites/<site>.yaml [--location NAME] [--supply FILE] [--limit N] [--record all|errors|off] [--sink URL ...] [--archive URL]
scrape replay runs/<site>/<run_id> [--config edited.yaml] [--sink URL ...] [--archive URL]
scrape inspect runs/<site>/<run_id> <capture_no> [--field name] [--body]
scrape fixture save runs/<site>/<run_id>          # -> tests/sites/<site>/
scrape diff runs/<site>/<old_id> runs/<site>/<new_id> [--field name] [--json]
scrape export runs/<site>/<run_id> --sink URL [--sink URL ...] [--force]
```

`diff` compares two Runs of one Site and Location, per Record Type with a Record Key in either Run's
`run.json` config (a key that differs between them is an error). It lists
Records added, removed and changed, each change with its top-level Fields' old and new
values (an absent Field compares as `null`); `--field` narrows changes to one Field, and
is an error when no Record in either Run has it.
Records are compared without `_meta`; Runs from before deduplication keep their first
Record per key, and Records with a null key Field are skipped. A Run that did not finish
(e.g. cut by `--limit`) is warned about, since Records it missed show as added or removed.
`--json` prints full added/removed Records and `{old, new}` per changed Field.

`inspect` reads the saved Capture and Extraction Trace without a crawl. It shows the HTTP
summary beside Field and Follow Rule Step outputs, labels errors, and prints the parent
chain (marking parents omitted by `record_level: errors`). `--field` limits the trace to
that Field and its nested paths. `--body` pretty-prints the response JSON/HTML, decoding
gzip/deflate first; malformed JSON is shown as text so parsing failures can be inspected.

`fixture save` requires a completed healthy Run without `--limit`, recorded at `all`.
It copies Captures and the Run manifest to `tests/sites/<site>/`, and writes expected
Records with volatile `_meta` fields (`run_id`, `scraped_at`, `capture_no`) removed,
retaining stable metadata such as Site, Record Type and source URL. Re-saving replaces
the fixture. The Site regression tests
Replay each fixture against the current `sites/<site>.yaml` with network connections
refused, comparing Records independently of crawl order and rejecting missing Captures.

## Sinks

The Run directory stays the primary output (`diff`, Replay and fixtures read it). A Sink
receives a copy once the Run has finished, so blocking I/O stays out of the crawl and the
export can be gated on Run Health: `failed` Runs are skipped unless `export --force`.
Sinks are chosen per invocation with `--sink` (a deployment concern, not Site config);
credentials come from the URL or the backend's usual environment. Each Sink is checked
(driver installed, reachable) before the crawl starts. Each export replaces any earlier
export of the same Run, so re-running `export` is safe. A failing Sink does not stop the
others; errors name Sinks without credentials.

Exit codes: `export` exits 1 for a skipped or unreadable Run and 3 when a Sink fails;
`run`/`replay` keep their Run Health code unless a Sink fails (3) or the Archive does (4).

- `postgres://…` (`groceries-scraper[postgres]`): one transaction replaces the Run's
  `scrape_runs` row (site, run_id, location, health, manifest) and its `scrape_records` rows
  (record_type, record_key, data without `_meta`, meta). Tables are created before the
  first export if missing; tables from before Locations gain `location` (`default` for
  their Runs). NUL characters, which jsonb rejects, become U+FFFD.
- `s3://<bucket>[/<prefix>]` (`groceries-scraper[s3]`): mirrors `run.json` and
  `records/*.jsonl` to `<prefix>/<site>/<location>/<run_id>/`, removing objects the Run no longer
  has; `run.json` is uploaded last as the completion marker.

## Archive

`run`/`replay --archive s3://<bucket>[/<prefix>]` (`groceries-scraper[s3]`) copies the whole
Run directory, byte for byte, to `<prefix>/<site>/<location>/<run_id>/` so it outlives the
machine that ran it. Unlike a Sink it is made for every Run, `failed` ones included, and
nothing is ever pruned (ADR-0003). It holds whatever `record_level` kept: `off` archives
only `run.json` and Records. `run.json` is uploaded last as the completion marker.

Order: an Archive URL overlapping an S3 `--sink` (same bucket, one prefix containing the
other) is refused (exit 1); the Sinks and the Archive are checked (an unusable Archive exits
4); the crawl; the Archive, which prints its URL; the Sinks. The check writes an empty
`<prefix>/.write-check`, so a read-only bucket fails before the crawl. The Archive goes first so a Run
stopped by SIGTERM is archived within the shutdown grace period. Credentials come from the
standard AWS environment, as for the S3 Sink. The bucket holds raw responses: keep it
private and encrypted.

## Deferred (post-v1)
Browser waits beyond network idle (e.g. for a selector) · a proxy per Session · price history across many Runs · nightly live smoke runs.
