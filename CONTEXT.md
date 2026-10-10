# Groceries Scraper

A config-driven scraping engine: each website to scrape is described declaratively in YAML, and the engine crawls and extracts data from it without site-specific code.

## Language

### Crawl structure

**Site**:
One scrape target (e.g. a single grocery retailer) described by one configuration.
_Avoid_: Target, spider, config

**Page Type**:
A named kind of page within a **Site** (e.g. `listing`, `product`) that shares one set of extraction and follow rules.
_Avoid_: Template, callback, route

**Start Request**:
An entry-point URL for a **Site**, tagged with the **Page Type** that handles it.
_Avoid_: Seed, target URL

**Follow Rule**:
A rule on a **Page Type** that discovers further URLs and names the **Page Type** that handles them (covers pagination and listing → detail).
_Avoid_: Link rule, crawl rule

**Request Template**:
The HTTP request a **Follow Rule** issues (method, URL, headers, body), rendered from **Variables**.
_Avoid_: Request spec, call config

### Extraction

**Record**:
One output object emitted by a **Page Type** (e.g. one product).
_Avoid_: Item, row, result

**Record Type**:
The name of a kind of **Record** (e.g. `product`, `promotion`); only **Page Types** declaring one emit **Records**.
_Avoid_: Entity, model, schema, item type

**Record Key**:
The **Field**(s) that identify the same real-world thing across **Runs** (e.g. `sku`), declared per **Record Type**.
_Avoid_: ID, primary key, unique key

**Record Contract**:
The **Fields**, types and required flags declared once for a **Record Type**, which every **Page Type** emitting it must match exactly.
_Avoid_: Schema, model

**Field**:
A named, typed value in a **Record** (or nested object), with a selector describing where to find it.
_Avoid_: Attribute, property, column

**Selector**:
An expression (CSS, XPath, or JSONPath) locating nodes within the current **Scope**.
_Avoid_: Query, path, locator

**Scope**:
The node against which a **Selector** is evaluated — the whole response, or the current node of an enclosing **Loop**.
_Avoid_: Context, root

**Loop**:
An `each` **Selector** that yields one **Scope** per matched node — producing one **Record** per match at page level, or one array element per match inside a **Field**.
_Avoid_: Iterator, repeat, foreach

**Pipe**:
An ordered list of **Steps** producing a value (or a **Loop**'s nodes); a single **Selector** is shorthand for a one-step **Pipe**.
_Avoid_: Chain, pipeline, processor

**Step**:
One operation in a **Pipe** — a **Selector**, a parse (switching **Scope** between HTML and JSON), a built-in transform, or a custom function.
_Avoid_: Filter, processor, operator

**Coercion**:
The strict conversion of a **Pipe**'s final value to its **Field**'s declared type; never cleans up input.
_Avoid_: Casting, parsing

### Session and state

**Location**:
A named store or postcode context, declared on a **Site**, under which a **Run** scrapes; it changes what data the **Site** returns.
_Avoid_: Locale, store, region, variant

**Session**:
One HTTP identity (cookies + session **Variables**) within a **Run**; it never changes what data is returned.
_Avoid_: Auth, login, context

**Session Pool**:
The interchangeable **Sessions** a **Run** spreads its crawl across.
_Avoid_: Workers, identity pool

**Session Setup**:
The ordered requests run once before **Start Requests** (and again on refresh) to establish the **Session**, e.g. fetching a CSRF token.
_Avoid_: Bootstrap, pre-flight, auth step

**Variable**:
A named value available to templates — session-scoped (`session.*`), location-scoped (`location.*`, fixed for the **Run**), or carried along a crawl chain by a **Follow Rule**.
_Avoid_: Param, context value, meta

### Runs and debugging

**Run**:
One execution of a **Site** configuration, identified by a run id, with its own output directory.
_Avoid_: Job, crawl, execution

**Job**:
Only the Kubernetes Job that runs one **Run**; never a synonym for the Run itself.

**Capture**:
The recorded raw HTTP exchange (request + response) for one request in a **Run**.
_Avoid_: Snapshot, dump, cache entry

**Extraction Trace**:
The per-**Capture** record of what each field and **Follow Rule** selector matched, converted to, or failed on.
_Avoid_: Debug log, extraction log

**Run Health**:
The outcome of a **Run** — `ok`, `degraded`, or `failed` — determined by its **Health Checks**.
_Avoid_: Status, result, success

**Health Check**:
A per-**Site** threshold on a **Run**'s statistics (e.g. minimum **Records**, maximum null ratio for a **Field**).
_Avoid_: Assertion, alert, monitor

**Replay**:
Re-running a **Run**'s crawl offline, serving responses from its **Captures** (matched by strict request fingerprint, minus the **Site**'s ignored params) instead of the network.
_Avoid_: Re-run, offline mode, cache mode

**Sink**:
A destination outside the **Run** directory (Postgres, S3) that a finished **Run**'s **Records** are exported to.
_Avoid_: Output, exporter, destination

**Archive**:
A copy of a finished **Run**'s whole directory (including **Captures** and **Extraction Traces**) in object storage, kept so the **Run** can be inspected or replayed after the machine that ran it is gone.
_Avoid_: Backup, Sink, upload

### Scheduling and the tracker

**Supplied Start Request**:
A **Start Request** handed to a **Run** from outside the **Site** configuration (e.g. a product URL from the **Watch List**), replacing the **Site**'s own `start:` list for that **Run**.
_Avoid_: Target, seed, external URL

**Accepts Rule**:
A **Site**'s declaration of which URLs it can take as **Supplied Start Requests** (a URL pattern), which **Page Type** handles them, and the retailer whose links they are.
_Avoid_: Targets, route, matcher

**Start Request Outcome**:
The result reported for one **Supplied Start Request** — `ok`, `failed`, `blocked`, `not_found` or `skipped` — so a missing **Record** is never mistaken for a missing product.
_Avoid_: Status, result

**Price Record contract**:
The **Fields** every **Record** of an **Accepts Rule**'s **Page Type** must declare so the **Dispatcher** can read it — `url`, `name` and `price` in dollars, plus optional price facts; unlike a **Record Contract** it allows other **Fields**.
_Avoid_: Price schema, tracker schema

**Schedule**:
A **Site**'s minimum interval between checks of the same **Supplied Start Request**, bounded by a global minimum.
_Avoid_: Cron, frequency config

**Dispatcher**:
The recurring process that turns the **Watch List** into due **Runs**, records their **Price Observations** and delivers **Price Changes** to the tracker.
_Avoid_: Scheduler, runner, orchestrator

**Watch List**:
The tracker's list of product URLs and **Locations** to keep checking; it says what to check, never when.
_Avoid_: Due list, targets, queue

**Site Catalogue**:
What the **Dispatcher** tells the tracker it can check: each **Site**'s **Accepts Rule** (with its retailer), **Locations** and **Schedule**.
_Avoid_: Capabilities, manifest

**Price Observation**:
One successful check of one product at one **Location**, kept in full in Postgres; never overwritten.
_Avoid_: Snapshot, price row

**Product State**:
The price-relevant facts of a **Price Observation** — shelf price, regular price, Deal flag, unit price, price kind and availability.
_Avoid_: Snapshot, status

**Price Change**:
A **Price Observation** whose **Product State** differs from the previous successful one for that product and **Location**; the only kind of observation sent to the tracker.
_Avoid_: Update, delta, event

**Check Health**:
Whether checks of one product at one **Location** currently succeed (`ok`) or fail (`failing`); only transitions are sent to the tracker.
_Avoid_: Status, Run Health

## Relationships

- A **Site** has one or more **Start Requests** and one or more **Page Types**
- A **Start Request** is handled by exactly one **Page Type**
- A **Page Type** has zero or more **Follow Rules**; each **Follow Rule** targets exactly one **Page Type** (may be itself, for pagination)
- A **Follow Rule** issues requests via a **Request Template** (default: GET the selected URL)
- A **Site** declares zero or more **Locations**; a **Site** without any has one implicit **Location**, `default`
- A **Run** scrapes exactly one **Location**
- A **Run** has one **Session Pool** of one or more **Sessions**; each **Session** runs its own **Session Setup**, which populates its session **Variables**
- A **Start Request** is assigned to one **Session**; every request followed from it uses that same **Session**
- A **Page Type** emits zero or more **Records** — one per **Loop** match, or exactly one if it has no page-level **Loop**
- A **Record** has one or more **Fields**; a **Field** of type object or array may contain nested **Fields**
- Every **Selector** is evaluated relative to its **Scope**; absolute XPath inside a **Loop** is rejected unless explicitly marked absolute
- A **Page Type** emits **Records** of at most one **Record Type**; a **Page Type** without one only navigates
- Data split across pages is combined by a **Follow Rule** passing **Variables** to the child **Page Type**, which emits the complete **Record** — the engine never merges partial **Records**
- A **Follow Rule** is evaluated once per page, or once per **Loop** node (so passed **Variables** belong to that node)
- A **Field** or **Loop** obtains its value through exactly one **Pipe**; **Coercion** applies after the last **Step**
- A **Record** missing a required **Field** is dropped and the reason recorded in the **Extraction Trace**
- A **Run** has many **Captures**; each **Capture** has one **Extraction Trace** and at most one parent **Capture**
- A **Run** has exactly one **Run Health**, derived from the **Site**'s **Health Checks** plus built-in failure conditions
- A **Run** that loses some of its **Sessions** hands their unstarted **Start Requests** to the rest and is at best `degraded`; losing all of them makes it `failed`
- A **Record Type** has at most one **Record Contract**; with one, every emitting **Page Type** declares exactly its **Fields**
- A **Record** belongs to its **Run**'s **Location**; the same **Record Key** in two **Locations** identifies two different facts
- A **Record Type** has at most one **Record Key**; within a **Run** only the first **Record** per key is kept
- Two **Runs** of a **Site** are compared by matching **Records** on their **Record Key**, only when both scrape the same **Location**
- A **Replay** reads the **Captures** of exactly one prior **Run** and produces a new **Run**
- A **Run** is exported to zero or more **Sinks** after it finishes; a **Run** with **Supplied Start Requests** is exported whatever its **Run Health**, other `failed` **Runs** only when forced, and re-exporting replaces the **Sink**'s earlier copy
- A **Run** has at most one **Archive**, made after it finishes whatever its **Run Health**
- A **Site** has at most one **Accepts Rule**; a URL matching two **Sites**' rules is a configuration error
- An **Accepts Rule** names one retailer, and no two **Sites** name the same one, because a Home Store's **Locations** come from one **Site**
- A **Run** given **Supplied Start Requests** ignores the **Site**'s `start:` list and reports exactly one **Start Request Outcome** per **Supplied Start Request**
- A **Site** with an **Accepts Rule** has a **Schedule**; the **Dispatcher** decides when each **Watch List** entry is due from its last **Price Observation**
- Each due (**Site**, **Location**) pair becomes one **Run**
- A **Price Observation** is a **Price Change** only when its **Product State** differs from the last successful one; a failed check is never a **Price Change**

## Example dialogue

> **Dev:** "The Dairy category URL — is that a **Start Request** for the `product` **Page Type**?"
> **Domain expert:** "No — it's a **Start Request** for `listing`. The `listing` **Page Type** has a **Follow Rule** into `product`, and another back into `listing` for the next page."
> **Dev:** "The listing tile has the price but the product page doesn't. Do both emit a `product` **Record** and we merge?"
> **Domain expert:** "No merging. The **Follow Rule** runs per **Loop** node and passes the price as a **Variable**; only `product` declares the `product` **Record Type**, and its price **Field** reads that **Variable**."
> **Dev:** "Walnuts were checked 48 times today at the same $5.49. Does the tracker get 48 rows?"
> **Domain expert:** "No. Postgres keeps all 48 **Price Observations**; the tracker gets a **Price Change** only when the **Product State** moves, plus a daily heartbeat. A timeout is a **Check Health** transition, never a **Price Change**."

## Flagged ambiguities

- "item" collides with Scrapy's `Item` and the `items:` config key — resolved: an emitted output object is a **Record**.
- "session" was used both for a store/postcode selection and for a parallel identity — resolved: the selection is a **Location** (per **Run**); identities are **Sessions** in a **Session Pool**.
- "target" was used for both the whole website and individual URLs — resolved: the website is a **Site**; entry URLs are **Start Requests**.
- "target" came back for URLs supplied by the tracker — resolved: they are **Supplied Start Requests**, and the **Site**'s declaration is its **Accepts Rule**.
- "Location" and the tracker's "Home Store" name the same store context from two sides — resolved: a **Home Store** references a **Location** by name; store selection details live only in the **Site**.
