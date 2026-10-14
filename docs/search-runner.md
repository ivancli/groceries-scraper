# Search runner

The supervised execution seam behind Product Search
([#86](https://github.com/ivancli/groceries-scraper/issues/86)). `Supervisor.search` takes
one validated search v1 request and runs it as a crawler child process under the shared
Search Admission ([traffic coordination](traffic-coordination.md)). The HTTP API and its
NDJSON framing ([#88](https://github.com/ivancli/groceries-scraper/issues/88)) call
`search` and turn its acceptance callback and `Outcome` into frames. Retailer adapters such
as Coles ([#87](https://github.com/ivancli/groceries-scraper/issues/87)) supply the
definitions it runs.

## Search Adapters

A Search Adapter is a Site file with a `search:` block and no `start`, `accepts` or
`schedule`. Adapters are loaded by `search.adapters`, never by the price configuration
loaders, so they appear in neither the Site Catalogue nor the Price Record contract, and
their Records never reach a Sink.

```yaml
search:
  adapter_version: coles-live-v1
  retailer: {key: coles, name: Coles, hosts: [coles.com.au]}
  enabled: false
  url: "https://www.coles.com.au/search/products?q={query}"  # the only Start Request
  page_type: search
  record: result
  max_results: 10
  verify:
    page: "//main[@id='search-results']"       # only a genuine search response matches
    empty: "//p[@data-testid='no-results']"     # only a genuine empty search matches
    blocked: "//title[contains(., 'Pardon Our Interruption')]"
    blocked_statuses: [401, 403, 429]
settings: {...}
page_types: {...}  # reuses Fields, Loops, Pipes and `render: browser`
```

The request supplies only the query. It is percent-encoded into the single `{query}` slot,
which can't be in the host, so a request can't choose the URL. The request's
`context.location` must be one of the Site's Locations, or `default` when it declares none.

## Outcomes

The child decides the outcome from the search page's last Capture, not from Run Health.
Run Health fails any declared Record Type with zero Records, which is the wrong rule for
search. The checks run in order:

| Check | Outcome |
| --- | --- |
| No Capture of the search page | `failed` `no_response` |
| Status in `blocked_statuses`, or `verify.blocked` matches | `blocked` |
| Any other status ≥ 400 | `failed` `http_<status>` |
| `verify.page` doesn't match | `failed` `unrecognized_page` |
| A Record dropped for a missing required Field | `failed` `corrupt_result` |
| Records | `ok`, in Record order, deduplicated by `url`, at most `max_results` |
| No Records, and `verify.empty` matches | `ok` with no results (verified empty) |
| No Records otherwise | `failed` `unverified_empty` |

A repeated product, which the Run drops by Record Key, isn't corruption.

The supervisor's own outcomes are:
- refusals before admission: `invalid_request`, `unsupported_retailer`, `busy`, `unavailable` and `timeout`;
- `timeout` at the deadline;
- `busy` when a permit can't be had in time;
- `failed`: `child_exited`, `protocol`, `result_too_large`, `stale_admission` or `unconfigured_retailer`.

## Budgets and retries

- **Request limits:** `budget_ms` may be at most 10 000 and `admission_budget_ms` at most 2 000. The execution's deadline is the smaller of `budget_ms` and the request's `expires_at`, and nothing extends it.
- **Admission:** must commit within the admission budget. An admission that commits after the caller has given up is ended at once; nothing ran under it.
- **Child budget:** the child gets the remaining time minus 0.25 s for delivery. That bounds `DOWNLOAD_TIMEOUT`, `CLOSESPIDER_TIMEOUT` and Playwright's navigation timeout.
- **One attempt:** Scrapy retries are off and Session refresh is disabled. Price Runs keep their own retry policy.
- **Pacing:** a permit waits for the retailer's next start only if that falls inside the deadline. If the retailer's in-flight limit is full, the search is `busy` at once.

## Isolation

- **Environment:** the child gets only `PATH`, `LANG` and `PLAYWRIGHT_BROWSERS_PATH`. `HOME` and `TMPDIR` point at a fresh scratch directory, which is also its working directory and is deleted afterwards. `SCRAPE_SINK`, `PGPASSWORD`, the coordinator and Dispatcher credentials are never passed on.
- **Browser:** each child launches its own browser with a new, non-persistent context, so no profile is ever loaded. Service workers are blocked, and images, media and fonts are aborted unsent. Other browser requests to the retailer each take a permit.
- **IPC:** one socket per execution carries the job, the permit requests and replies, and one result line of at most 64 KiB. The child asks the supervisor for each permit, and the supervisor takes it under the admitted execution's fence. A child never names a retailer, never holds a credential, and gets nothing after a recovery (`stale_admission`).

## Cancellation and cleanup

- **Coalescing:** callers with the same `execution_key` and identical input share one execution. A joining caller doesn't extend its deadline, and it stops waiting (`timeout`) at its own. The same `execution_key` or request id with different input is refused. A finished request id is refused for 15 minutes in this process. Across restarts, admission ids, which are the admitting request ids, can't be reused.
- **Detach:** cancelling a caller's `search` detaches it. When the last caller detaches, the execution stops and can no longer be joined: the same work starts afresh, and is Busy until cleanup ends. If every caller leaves during admission, no child is started.
- **Stopping the child:** at the end of every execution, the child's whole session gets SIGTERM, then SIGKILL after the grace period. The child is the leader of a new session, so its browser and any other descendants are included. Admission ends only once `/proc` shows nothing left running in that session.
- **When stopping fails:** if anything survives, admission stays held and the operator recovers it ([recovery](traffic-coordination.md#recovery-authority)).
- **Restarts:** nothing is persisted, so a restart never replays work.
- **Shutdown:** await `Supervisor.idle()` so that in-progress cleanups finish before the event loop closes.

## Deployment (#88)

- **One replica:** run a single API replica with the `Recreate` strategy, so an old and a new Pod never supervise at once. Fencing still guards an overlap.
- **Credentials:** the Pod gets only `scrape-coordinator` and the origin credential: no Kubernetes token and no `scrape-sinks`.
- **Root filesystem:** read-only, apart from a writable scratch volume for child directories.
