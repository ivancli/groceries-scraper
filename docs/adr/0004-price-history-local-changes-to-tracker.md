# Full price history stays in local Postgres; only Price Changes reach the tracker

groceries-tracker (Cloudflare Worker + D1, free plan) stops fetching retailers; this scraper collects for it. Every **Price Observation** is kept in full in the scraper's Postgres on the local Kubernetes cluster. The tracker receives only **Price Changes**, **Check Health** transitions and one heartbeat per product and **Location** a day. They are delivered through an outbox table that keeps each item until the tracker acknowledges it.

D1's free plan allows 100,000 rows written a day and 500 MB per database, and over-limit writes fail. Sending every check of 1,000 products every 30 minutes is about 96,000 rows a day and fills 500 MB in about five weeks. Sending only changes is about 3,000 rows a day. Grocery prices move once or twice a week, so almost every check would only repeat the last one.

## Considered Options

- **Every observation in D1**: rejected; it exceeds the free plan at the intended check frequency.
- **The tracker reads this Postgres directly** (through a Cloudflare Tunnel): rejected. The app would stop working whenever the PC is off, a home database would be exposed to the internet, and Deal Episode logic would have to move into Python. The tracker would still need product identities, so nothing would stop being duplicated.
- **SQLite instead of Postgres**: rejected. Concurrent Runs as Kubernetes Jobs (ADR-0003) need a server database, the Postgres Sink already exists, and a hosted runner later only needs a new connection string.

## Consequences

- The scraper's Postgres is the system of record for price history; back it up. D1 holds a compact history of moves only.
- Change detection runs in the **Dispatcher** after each Run, against a latest-state table in Postgres. A failed check is never a **Price Change**, so Deal Episodes in the tracker behave exactly as if every check had been sent.
- Every delivered item has a deterministic id (Site, Location, product URL, observed time). The tracker ignores duplicates and anything older than its current state, so retrying is always safe.
- The tables are separate from the generic `scrape_runs`/`scrape_records` Sink tables. The Sink stays a generic Record export.
