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

## Check supplied product URLs

A Site with an Accepts Rule (`accepts:`) can check a list of product URLs instead of its
`start:` list. Each line of the supply file is `{"ref": "…", "url": "…"}`:

```bash
uv run scrape run sites/<site>.yaml --supply supply.jsonl
```

Records carry `_meta.ref`, and `outcomes.jsonl` in the Run directory reports one of `ok`,
`not_found`, `blocked`, `skipped` or `failed` per ref. A URL the Site doesn't accept is
refused before anything is fetched. See [Supplied Start Requests](docs/design.md#supplied-start-requests).

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
saved Run later. Runs with Supplied Start Requests are exported whatever their Run
Health; other failed Runs are skipped unless `--force`. Re-exporting replaces the
earlier copy, including Start Request Outcomes in Postgres. Sinks are checked before
the crawl; a Sink failure exits `3`. Without `--sink`, `run` and `replay` read
whitespace-separated Sink URLs from `SCRAPE_SINK`.

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

## Run in a container

One image runs every Site. It holds the CLI with the `postgres` and `s3` extras,
the Chromium build matching the locked Playwright, `defaults.yaml` and `sites/`.
It runs as a non-root user with `scrape` as the entrypoint and writes Runs under
`/app/runs`. Build it locally from the repository root:

```bash
docker build -t groceries-scraper:dev .
docker run --rm groceries-scraper:dev validate sites/aldi.yaml
docker run --rm -v "$PWD/runs:/app/runs" groceries-scraper:dev run sites/aldi.yaml --limit 5
```

The `runs` mount keeps the Run on the host. It must be writable by the image's user
(UID 10001), or pass `--user "$(id -u)"` to write as yourself. Site configs are baked in,
so rebuild after editing them, or mount `-v "$PWD/sites:/app/sites:ro"`.

CI builds the image on every pull request and checks it offline: `validate sites/` (every
Site, and no two accepting the same URL) and a Replay of the ALDI Golden Fixture. On `main` it pushes
`ghcr.io/ivancli/groceries-scraper:<sha>` and `:main`.

## Run as a Job on a local k3d cluster

Each Run can execute as one Kubernetes Job. A Kustomize overlay holds the cluster's
settings and `scrape deploy job` adds the Site and Location
([ADR-0003](docs/adr/0003-kubernetes-jobs-with-archives.md)). The local overlay
([`deploy/k8s/overlays/local`](deploy/k8s/overlays/local)) needs
[k3d](https://k3d.io/) and `kubectl`. Its node maps this checkout's `runs/` and `sites/`,
so a Job's Run lands in `./runs` and an edited Site config applies to the next Job
without rebuilding the image. From the repository root:

```bash
mkdir -p runs backups  # must exist first, or Docker creates them owned by root
k3d cluster create groceries \
  -v "$PWD/runs:/mnt/groceries-scraper/runs@all" \
  -v "$PWD/sites:/mnt/groceries-scraper/sites@all" \
  -v "$PWD/backups:/mnt/groceries-scraper/backups@all"
kubectl apply -k deploy/k8s/overlays/local/postgres
docker build -t groceries-scraper:dev .
k3d image import groceries-scraper:dev -c groceries
job=$(kubectl kustomize deploy/k8s/overlays/local \
  | uv run scrape deploy job sites/aldi.yaml \
  | kubectl create -f - -o name)
kubectl logs -f "$job" --pod-running-timeout=2m
```

The Run directory appears as `runs/aldi/<run_id>/`, ready for `scrape inspect` and
`replay`. Re-import the image after changing code; the mappings are fixed when the cluster
is created, so another checkout needs its own cluster. The pod runs as UID/GID 1000 so
the files are yours; if `id -u` or `id -g` differ, change `runAsUser`/`runAsGroup` in
[`deploy/k8s/overlays/local/job.yaml`](deploy/k8s/overlays/local/job.yaml) and
[`postgres/backup.yaml`](deploy/k8s/overlays/local/postgres/backup.yaml).
Every Job exports to the cluster's Postgres through `SCRAPE_SINK` and `PGPASSWORD` from the
`scrape-sinks` Secret; that Secret is optional, so skipping the Postgres step leaves Jobs
without a Sink. The local overlay makes no Archive.

The Job succeeds only for an `ok` Run. Exit codes 1–4 (degraded, failed, Sink or
Archive failure, but also an uncaught error, which exits 1) fail it at once; other pod failures retry up to twice, and evicted
pods don't count. `kubectl get jobs -l groceries-scraper/site=aldi` shows the outcome,
and `k3d cluster delete groceries` removes the cluster.

### Dispatch the tracker's Watch List

`scrape dispatch` runs every 5 minutes as a CronJob
([`deploy/k8s/overlays/local/dispatcher`](deploy/k8s/overlays/local/dispatcher)). Each tick
ingests finished dispatched Runs, delivers Price Changes to the tracker, refreshes the
Watch List, publishes the Site Catalogue, and creates one Job per due (Site, Location)
([ADR-0005](docs/adr/0005-dispatcher-schedules-supplied-start-requests.md)). It needs the
Postgres step above, the tracker's Cloudflare Access service token, and the Job template
it renders from:

```bash
kubectl create secret generic scrape-dispatcher \
  --from-literal=TRACKER_URL=https://<tracker> \
  --from-literal=CF_ACCESS_CLIENT_ID=<id> --from-literal=CF_ACCESS_CLIENT_SECRET=<secret>
kubectl kustomize deploy/k8s/overlays/local \
  | kubectl create configmap scrape-job-template --from-file=job.yaml=/dev/stdin \
      --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -k deploy/k8s/overlays/local/dispatcher
kubectl create job --from=cronjob/scrape-dispatch scrape-dispatch-now  # tick now
```

Re-run the `scrape-job-template` line after changing the local overlay. The Dispatcher's
ServiceAccount may only create and get Jobs and ConfigMaps. Its Jobs are labelled like
manual ones, so `kubectl get jobs -l groceries-scraper/site=aldi_picks` shows them.

### Clean up dispatched Runs

`scrape janitor` runs daily at 03:30 UTC (or when the PC is next on) as a CronJob
([`deploy/k8s/overlays/local/janitor`](deploy/k8s/overlays/local/janitor)). It deletes
dispatched Run directories under `runs/` after 14 days, or 60 days for `failed`,
`degraded` and unfinished ones, and the `scrape_records` and `scrape_outcomes` rows of
ingested dispatched Runs after 14 days. Runs it didn't dispatch, `scrape_runs`, checks and
Price Observations are kept. To see what it would delete, then sweep now:

```bash
kubectl apply -k deploy/k8s/overlays/local/janitor
SCRAPE_SINK=postgresql://… uv run scrape janitor --dry-run  # from the repository root
kubectl create job --from=cronjob/scrape-janitor scrape-janitor-now
```

### Back up and restore the cluster's Postgres

This database is the system of record for price history
([ADR-0004](docs/adr/0004-price-history-local-changes-to-tracker.md)). Its volume survives
the pod but not `k3d cluster delete`, so a CronJob dumps it nightly at 03:00 UTC (or when the
PC is next on) to `backups/scrape-<time>.dump`, keeping the newest 14. To back up now:

```bash
kubectl create job --from=cronjob/postgres-backup postgres-backup-now
```

To restore a dump into a new database beside `scrape` and check it, then into a fresh
cluster's empty `scrape` with `db=scrape` and no `createdb`:

```bash
dump=backups/scrape-<time>.dump db=restored
kubectl exec postgres-0 -- createdb -U scrape "$db"
kubectl exec -i postgres-0 -- pg_restore -U scrape -d "$db" --no-owner < "$dump"
kubectl exec postgres-0 -- psql -U scrape -d "$db" -c \
  'SELECT (SELECT count(*) FROM scrape_runs) runs, (SELECT count(*) FROM scrape_records) records'
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
