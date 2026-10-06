# A Dispatcher in this repo turns the tracker's Watch List into scheduled Runs

The tracker publishes a **Watch List**: product URLs plus **Location** names, with no timings. `scrape dispatch` runs as a Kubernetes CronJob on the local cluster. It decides what is due from each **Site**'s **Schedule** and the last **Price Observations** in Postgres. It then creates one Job per due (Site, Location) through the `scrape deploy job` renderer, with the due URLs as **Supplied Start Requests**. It also publishes a **Site Catalogue** back to the tracker.

The tracker can no longer tell what is due, because it only hears about changes (ADR-0004). Timing is collection policy, which belongs to the scraper, so a Site's check frequency sits beside its download delay. Catching up after the PC was off needs no extra mechanism, because everything is simply overdue.

## Decisions within this

- **Schedule lives in the Site YAML** (`schedule: {every: 30m}`), with a global minimum in `defaults.yaml`. This reverses the provisional note on #44 that schedules stay out of Site YAML. Frequency is a per-retailer politeness setting like `download_delay`, not a cluster concern.
- **Routing is by URL, not retailer name.** A Site's **Accepts Rule** (`accepts: {page_type, url}`) claims URLs, and a URL claimed by two Sites fails validation. The scraper never learns the tracker's retailer names.
- **Supplied Start Requests replace `start:`.** Every one of them gets a **Start Request Outcome**, because missing Records cannot tell "not found" from "robots-denied" or "crashed". An Accepts Rule's Page Type must not follow links, so a supplied URL never turns into a crawl.
- **Home Stores reference named Locations** declared in the Site. Store selection details never leave the Site, and ADR-0002's named, stable Location per Run holds.
- **The Dispatcher is the only tracker-aware code.** It lives in `groceries_scraper/dispatch/`, ships in the same image, and speaks the tracker's Collector API. The engine, Sites and Sinks stay generic.

## Considered Options

- **The tracker computes due lists**: rejected; it would need to hear about every check.
- **A generated CronJob per (Site, Location)**: rejected; it can't catch up or back off on failures.
- **A separate integration repo**: rejected; it would need a second image and duplicated Kubernetes and Postgres code.
