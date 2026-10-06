# Tracker integration: tech plan

Status: design accepted 2026-10-06. Implemented so far: `scrape run --supply` and the Accepts Rule (#56); Schedules, the Price Record contract and `aldi_picks` opting in (#57); the Dispatcher store (#59), Collector client (#60) and `scrape dispatch` (#61). Decisions: [ADR-0004](adr/0004-price-history-local-changes-to-tracker.md), [ADR-0005](adr/0005-dispatcher-schedules-supplied-start-requests.md). The Collector API contract is owned by the tracker: [groceries-tracker `docs/scraper-integration.md`](https://github.com/ivancli/groceries-tracker/blob/main/docs/scraper-integration.md). Terms: [CONTEXT.md](../CONTEXT.md). This resolves #44.

## Flow

```mermaid
sequenceDiagram
  participant T as Tracker (Worker)
  participant D as scrape dispatch (CronJob, every 5 min)
  participant K as Kubernetes
  participant R as scrape run (Job)
  participant P as Postgres
  D->>P: ingest finished Runs → Price Observations, latest state, outbox
  D->>T: POST /api/collector/changes (outbox, until acked)
  D->>T: GET /api/collector/watch-list (If-None-Match)
  D->>T: PUT /api/collector/site-catalogue (when changed)
  D->>D: due = Schedule, backoff, check_soon, nothing in flight
  D->>K: per due (Site, Location): ConfigMap of Supplied Start Requests + Job
  K->>R: scrape run --supply … --record errors --sink postgres
  R->>P: Records + Start Request Outcomes (Postgres Sink)
```

A tick is idempotent and has `concurrencyPolicy: Forbid`. After the PC was off, the first tick finds everything overdue and catches up, with no extra mechanism.

## Site configuration

```yaml
site: aldi_picks
schedule: {every: 30m}            # optional `enabled: false` pauses the Site (the kill switch)
accepts:
  page_type: product
  url: '^https://www\.aldi\.com\.au/product/'
locations:                        # optional; `label` is shown in the tracker's Home Store picker
  sydney_g412: {label: "Sydney G412", service_point: G412}
```

Validation (`scrape validate`):
- A Site with `accepts` must have `schedule`, and vice versa.
- The accepted Page Type has no Follow Rules.
- The pattern compiles in the shared Python/JavaScript subset: anchored, no look-behind, no named groups.
- `every` is at least `defaults.yaml` `schedule.min_every` (15m).
- `scrape validate sites/` also rejects two Sites whose patterns claim the same URL. This is checked against example URLs listed with each Site (`accepts.examples`), because regex overlap can't be decided in general.

**Price Record contract.** An accepting Site's Record Type must declare these Fields:
- required: `url`, `name`, `price` (number, dollars)
- optional: `brand`, `size`, `regular_price`, `is_deal`, `unit_price`, `unit_basis`, `unit_price_text`, `price_kind`, `availability`, `store_verified`, `promo_text`

The Dispatcher converts dollars to cents with decimal arithmetic and rejects non-AUD values. A missing optional Field means "no evidence" (`null`), never `false`. `aldi_picks.yaml` gains `accepts`, `schedule` and `unit_price_text`.

## `scrape run --supply FILE`

- FILE is JSONL: `{"ref": "...", "url": "..."}`.
- The supplied requests replace `start:` and use the Accepts Rule's Page Type.
- Every Record carries `_meta.ref`. The Run writes `outcomes.jsonl`, one line per ref:

| Outcome | When |
|---|---|
| `ok` | at least one Record |
| `not_found` | 404/410 |
| `blocked` | 403/429, or a challenge detected by the Site's Health Checks |
| `skipped` | denied by robots, or never started before shutdown |
| `failed` | anything else, with the error |

- `run.json` records `supplied: true` and the ref count.
- The Postgres Sink gains a `scrape_outcomes` table. A supplied Run is exported whatever its Run Health, because outcomes are what the Dispatcher needs.
- Replay of a supplied Run reuses its supply, and its Records keep their original `scraped_at`. A Replay never feeds the Dispatcher: it ingests only Runs it created itself.

## Dispatcher store (Postgres schema `dispatch`)

| Table | Purpose |
|---|---|
| `watch_entries` | cached Watch List (`ref`, `url`, `location`, `check_soon`), plus its ETag |
| `dispatches` | one row per created Job: Site, Location, Job name, refs, created / finished / ingested |
| `checks` | every attempt: ref, Run, outcome, error, time (small; kept indefinitely) |
| `price_observations` | every successful check: Product State columns in cents, plus name/brand/size, Run id, Capture number |
| `latest_state` | per (`ref`): last Product State hash, `last_success_at`, health, `failing_since`, consecutive failures, last heartbeat date |
| `outbox` | items for the tracker: deterministic `id`, `kind`, payload, `created_at`, `sent_at`, attempts, last error |

**Ingesting a finished Run.** One transaction per Run:
1. Write the outcomes to `checks`.
2. For `ok`, insert into `price_observations`, compare with `latest_state` and enqueue a `change` if the state differs.
3. A move between `ok` and `failing` enqueues a `health` item.
4. The first success of the Sydney day, or any success while `check_soon` is set, enqueues a `heartbeat`.
5. `no_site`, `no_home_store`, `unknown_location`, `not_found` and `blocked` are each enqueued once per distinct value as `outcome` items.

Item ids are deterministic: `sha256(kind, site, location, ref, observed_at)`.

The store is implemented in `groceries_scraper.dispatch.store.DispatchStore` (#59).
`prepare()` creates the six tables above without changing the generic Sink tables.
`record_dispatch(site=…, location=…, run_id=…, job_name=…, refs=…, created_at=…)`
registers a Run before ingestion. `ingest(site, run_id)` reads its completed Postgres
Sink export in one transaction and returns `true` once; an unregistered Run,
Replay, unfinished Run or already ingested Run returns `false`. A Location or ref
mismatch raises an error and commits nothing. Successful checks use the Record's
`_meta.scraped_at`; unsuccessful checks use the Sink outcome's `at` timestamp.
Missing currency is treated as AUD; an explicit non-AUD currency is a failed check.
Dollar amounts use `Decimal` and round half up to integer cents.

The hash input is the UTF-8 encoding of the compact JSON array
`[kind, site, location, ref, observed_at]`, with the time normalised to UTC (`Z`).
Outbox payloads carry the full Collector API item, including `id`, `kind` and `ref`.
The first success establishes a Product State (`change`) without a health transition.
The first failure establishes failing Check Health. Only the five Collector API
outcome values listed above enter the outbox, once per distinct value per ref;
`failed` and `skipped` attempts are reported through Check Health. Repeated errors
still add checks and increment the failure streak; success resets that streak.
`check_soon` stays set until the Watch List is refreshed from the tracker.
Delayed Runs still add checks and Price Observations, but older successful states
cannot replace newer ones and older attempts cannot replace current Check Health.
Daily heartbeats are de-duplicated against the outbox, including already sent items.

`tests/test_dispatch_store.py` exercises the store against disposable Postgres.
Its walnut week has three checks per day, prices $5.49 → $4.49 → $4.99 → $5.49,
and one failed check followed by recovery: 21 checks, 20 Price Observations,
four changes, two health transitions and seven heartbeats.

**Collector client (#60).** `groceries_scraper.dispatch.collector.CollectorClient`
takes the tracker origin URL and a prepared `DispatchStore`. It reads
`CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET` from the environment and sends
them as the Cloudflare Access service-token headers. Responses and network error
details are never included in logs, and HTTP redirects are refused.

- `deliver_changes(dry_run=False)` drains the outbox oldest first, at most 100
  items per POST, and returns the count finalised. Both `acked` and `rejected`
  items get `sent_at`; rejected reasons are kept in `last_error`.
  Failed requests leave items pending, increment `attempts` and set
  `next_attempt_at`: 60 seconds, doubling per failure, capped at one hour.
  Backoff on an earlier batch stops later batches overtaking it. HTTP 400 stops
  the tick with a critical log. Invalid acknowledgements finalise nothing.
- `deliver_changes(dry_run=True)` posts every pending batch with `?dry_run=1`,
  returns zero and leaves all delivery state unchanged, including on failure.
  The `--dry-run` flag on `scrape dispatch` (#61) will pass this option through.
- `refresh_watch_list()` atomically replaces the cached entries and ETag after a
  valid response. HTTP 304 changes nothing. `store.watch_list()` reads the cache.
- `publish_site_catalogue(Path("sites"))` publishes only Sites with Accepts Rules
  and Schedules, including disabled Sites. Location Variables stay local; only
  names and labels are sent. Missing labels fall back to the Location name, and
  a Site without Locations publishes `default` with `store_specific: false`.
  The hash covers only the public catalogue and is saved after a successful PUT.

The Watch List ETag (including for an empty list) and catalogue hash live in
`dispatch.collector_state`; retry times live on the outbox. They survive fresh
CronJob processes. `DispatchStore.prepare()` upgrades an existing store without
discarding its history. `tests/test_collector.py` uses a fake HTTP tracker and
disposable Postgres (`TEST_DATABASE_URL`), including the 250-item interrupted
delivery and recovery scenario.

**Due rule.** An entry is due when nothing is in flight for its ref and either:
- `check_soon` is set and at least `min_every` has passed since its last attempt, or
- `every` has passed since its last success.

After a failure, the next try waits `every × 2^(n-1)`, capped at 24 hours. Due entries are grouped by (Site, Location) and chunked at 200 refs per Job.

**`scrape dispatch` (#61).** `groceries_scraper.dispatch.dispatcher.Dispatcher.tick(now)`
runs the five steps in order. In-flight means a dispatch not yet ingested. A Job that
has finished or disappeared without an ingestable Run, or whose Run cannot be ingested,
is abandoned: each of its refs gets a `failed` check, which feeds Check Health and
backoff. The Dispatcher assigns the Run id (`scrape run --run-id`) and the Job name,
records the dispatch, then creates the Job, then its ConfigMap (the pod waits for the
volume). A Job that could not be created cancels its dispatch. Dispatched Jobs get
`backoffLimit: 0`, since a second pod would reuse the Run id; the next tick retries
instead. A URL repeated within one (Site, Location) waits for a later tick, because a
Run fetches each URL once. Unroutable entries are reported through
`DispatchStore.report_outcome`, once per distinct outcome. `--dry-run` posts the outbox
with `?dry_run=1`, logs the Jobs it would create, and changes nothing else.
`tests/test_dispatch.py` drives ticks on a fake clock against disposable Postgres, the
fake tracker and an in-memory cluster.

**Kubernetes.**
- Each Job's supply lives in a ConfigMap, with an `ownerReference` to the Job so they're cleaned up together.
- Jobs are rendered by `scrape deploy job` (#48) with `--supply`, `--record errors` and the Postgres sink.
- The Dispatcher's ServiceAccount may create and get Jobs and ConfigMaps in its namespace, and nothing else.
- Tracker credentials (`CF-Access-Client-Id`/`Secret`, the tracker URL) come from Secret `scrape-dispatcher`.

## Retention

- Dispatched Runs use `record_level: errors`; manual Runs keep `all`.
- A janitor CronJob deletes Run directories after 14 days, and `failed`/`degraded` ones after 60 days.
- It also deletes the generic `scrape_records` rows of ingested dispatched Runs after 14 days. `price_observations` and `checks` are kept.
- A nightly `pg_dump` CronJob writes to a host path. This Postgres is the system of record for price history (ADR-0004).

## Local cluster additions (local overlay, #50)

- Postgres 16 StatefulSet on a `local-path` PVC.
- The `scrape-sinks` Secret pointing at it.
- The dispatcher CronJob, the janitor CronJob and the backup CronJob.

## First end-to-end slice

ALDI at `default`: a product linked in the tracker becomes a Watch List entry, then a dispatched `aldi_picks` Run, then a Price Observation in Postgres, then a `change` in the outbox, then a Price Change in D1, shown with its Last Checked time. A Replay or fixture price change produces exactly one new `change`. Locations, the janitor and backups follow.

## Tickets

Tracked under the `tracker-integration` label. Parent: #44.
