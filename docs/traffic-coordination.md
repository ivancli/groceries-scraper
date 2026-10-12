# Aggregate retailer traffic

Price Jobs and Product Search share each retailer's request limits through the existing
cluster Postgres ([#85](https://github.com/ivancli/groceries-scraper/issues/85), following
the approved [implementation handoff](https://github.com/ivancli/groceries-tracker/blob/8202bfdcd7265b41fe34bdb042cebd44ae435d2a/docs/product-search-implementation.md)).
A Site's own `download_delay` and `concurrent_requests_per_domain` bound one crawl only;
two overlapping Jobs, or a Job and a search, would each spend them in full. The Dispatcher
CronJob's `concurrencyPolicy: Forbid` serializes Dispatcher ticks, not the crawler Jobs
they create.

## Coordination store

Schema `coordination` ([`schema.sql`](../src/groceries_scraper/coordination/schema.sql)),
created by `scrape coordinator setup` as the database owner:

| Object | Holds |
| --- | --- |
| `search_executions` | Every admitted fresh search: work identity, owner, fence, deadline, and how it ended (`cleanup` or `recovery`). A partial unique index allows one unended row: the fresh-search slot. |
| `retailer_policies` | Per retailer, the strictest accepting Site's interval and concurrency, written by `setup`. |
| `retailer_pace` | Per retailer, when the next paced request may start. |
| `permits` | Requests in flight. A price Job's permit has a lease; a search's belongs to its execution. |

The restricted role (`scrape_coordinator`, Secret `scrape-coordinator`) has `USAGE` on the
schema and `EXECUTE` on `admit_search`, `end_search`, `acquire_permit` and
`release_permit` only. `acquire_permit` holds a share lock on the search execution's row
until it commits, so an execution that is ending can't be granted a permit that would
outlive it. These `SECURITY DEFINER` functions are the whole interface: the role
has no table privileges, so it can't read Price Observations, the Dispatcher store or Sink
tables, can't edit permits or policies directly, and can't call `recover_search`. Tests
check each of these refusals.

## Fresh-search admission

`admit_search(execution, work, owner, deadline)` either admits or answers at once:

- `busy`: another execution holds the slot. There is no queue.
- `replayed`: the id is known for other work or another owner, or has ended. An ended
  execution never runs again; ids stay refusable for a day past their deadline, after which
  the deadline alone refuses them.
- `expired`: the deadline has passed.

Re-admitting the same execution by the same owner returns its existing fence and deadline,
never a later one. The fence increases with every admission; a permit, or the owner's
`end_search`, with a stale fence is refused. A search gets no permits after its deadline.

The slot is freed only by `end_search`, which the owner calls after verifying that the
execution's process tree and browser are gone, or by operator recovery. Deadline expiry
alone frees nothing. Any coordinator error raises `CoordinatorUnavailable`, which callers
treat as refusal.

## Retailer permits

`acquire_permit` locks the retailer's pace row, so grants are serialized across Jobs and
search. The effective limits are the stricter of the caller's Site policy and the
retailer's configured policy, so neither search nor a lenient Site can raise them. A grant
returns a permit or an instruction to wait: until the next start, or until a request in
flight ends.

| Request | Permit |
| --- | --- |
| Scrapy downloads: pages, robots.txt, Session Setup, each redirect hop and retry | Paced and counted in flight, taken in the download handler as it sends |
| Browser navigations (`is_navigation_request()`) | Paced and counted in flight, taken in Playwright's route handler |
| Browser scripts, fetch/XHR, images and other subresources to the retailer | Counted in flight only, as a Site's `download_delay` never paced them |
| Browser redirect hops (Playwright doesn't route them) | Charged once sent: counted regardless of limits, and the pace owes their interval |

A request is the retailer's when its host is one of the Accepts Rule's `retailer.hosts`
or a subdomain of one. Without `hosts`, the hosts of `accepts.examples` are used. Requests
to other hosts aren't the retailer's traffic and aren't gated. Permits are released when
the request finishes or fails, or when its page closes. A permit granted after its page
closed is released at once. Only the permit's holder can release it. The browser context blocks service
workers because their requests bypass page routing. Not covered: WebSockets,
dedicated-worker requests and popup pages. No current Site uses them. A search adapter that needs them must
block them, or extend this coverage first.

A price Job waits for its permits. That changes only when it sends, not its collection or
retry rules. If the coordinator is unreachable, its requests fail instead of going out
uncoordinated, and Scrapy doesn't retry them. This is the one deliberate change: a
coordinator outage fails those refs, which then follow the Dispatcher's existing failure
backoff. Only accepting Sites, the ones the Dispatcher schedules, are coordinated. A manual
`scrape run` of a listing Site such as `aldi.yaml` is not, so don't run one while search is
enabled. A crashed Job's permits
lapse after their five-minute lease, which outlasts Scrapy's 180-second download timeout.
A search never waits, and gets no permits for a retailer without a configured policy: the
supervisor
([#86](https://github.com/ivancli/groceries-scraper/issues/86)) gets permits for its child
over IPC, so the child never holds the coordinator credential, and treats a wait as Busy.
A search's permits end only with its execution. Blocking reviewed unnecessary resource
types for search belongs to that runner.

## Measured bounds

`tests/test_coordinated_crawl.py` runs real `scrape run` processes against a fake retailer
on localhost, with disposable Postgres. Two overlapping price Runs of 4 pages each (Site
interval 0.3 s, 2 in flight) send 11 requests, including both robots.txt fetches and a
redirect hop:

| | Smallest gap between requests | Most in flight |
| --- | --- | --- |
| Coordinated | 0.2996 s (three runs) | 1 |
| Uncoordinated, same Runs | 0.010–0.013 s | 2 |

Each request reached the retailer while holding a permit, and none outlived its Run. A
browser Run's redirected navigation, script, fetch and image each took exactly one permit.
These are fixture measurements on one machine, not deployed latency.

## Recovery authority

Only the operator can free the slot of an owner that can no longer report cleanup, because
only the operator can verify that the owner terminated. The search API gets no Kubernetes
credentials for this.

1. `scrape coordinator status` shows the execution, fence and owner (the API Pod).
2. Confirm the owner is gone: `kubectl get pod <owner>` reports NotFound, or the Pod's
   containers have terminated. If termination can't be verified, leave the slot held.
   Search stays Busy, which is the intended fail-closed state.
3. `scrape coordinator recover <execution> --fence <n> --owner <owner> --owner-terminated
   --note "<why>"`. This is refused unless the execution, fence and owner still match.

Recovery deletes the execution's permits. A late `end_search` from the recovered owner is
refused by its stale fence, and its execution id cannot be admitted again.

## Rollout

Aggregate control holds only once every running price Job is coordinated. A new Job
template doesn't change Pods that are already running.

1. Create the Secret and run `scrape coordinator setup` (see the README).
2. Import the updated image and refresh the `scrape-job-template` ConfigMap, so new Jobs
   mount `scrape-coordinator`.
3. Drain old Jobs. Wait until no Job created before step 2 is still active (`kubectl get
   jobs -l app.kubernetes.io/name=groceries-scraper`). The Dispatcher keeps ingesting their
   Runs as usual.
4. Only then treat aggregate control as active and enable search.

The Secret is optional in the Job template, so a cluster without it keeps collecting
prices uncoordinated. Search must not be enabled on such a cluster.
