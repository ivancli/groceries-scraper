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

**Session**:
The single shared HTTP identity (cookies + session **Variables**) used for one **Site** run.
_Avoid_: Auth, login, context

**Session Setup**:
The ordered requests run once before **Start Requests** (and again on refresh) to establish the **Session**, e.g. fetching a CSRF token.
_Avoid_: Bootstrap, pre-flight, auth step

**Variable**:
A named value available to templates — either session-scoped (`session.*`) or carried along a crawl chain by a **Follow Rule**.
_Avoid_: Param, context value, meta

### Runs and debugging

**Run**:
One execution of a **Site** configuration, identified by a run id, with its own output directory.
_Avoid_: Job, crawl, execution

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

## Relationships

- A **Site** has one or more **Start Requests** and one or more **Page Types**
- A **Start Request** is handled by exactly one **Page Type**
- A **Page Type** has zero or more **Follow Rules**; each **Follow Rule** targets exactly one **Page Type** (may be itself, for pagination)
- A **Follow Rule** issues requests via a **Request Template** (default: GET the selected URL)
- A **Site** has at most one **Session** per run (v1); **Session Setup** populates its session **Variables**
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
- A **Record Type** has at most one **Record Contract**; with one, every emitting **Page Type** declares exactly its **Fields**
- A **Record Type** has at most one **Record Key**; within a **Run** only the first **Record** per key is kept
- Two **Runs** of a **Site** are compared by matching **Records** on their **Record Key**
- A **Replay** reads the **Captures** of exactly one prior **Run** and produces a new **Run**
- A **Run** is exported to zero or more **Sinks** after it finishes; a `failed` **Run** only when forced, and re-exporting replaces the **Sink**'s earlier copy

## Example dialogue

> **Dev:** "The Dairy category URL — is that a **Start Request** for the `product` **Page Type**?"
> **Domain expert:** "No — it's a **Start Request** for `listing`. The `listing` **Page Type** has a **Follow Rule** into `product`, and another back into `listing` for the next page."
> **Dev:** "The listing tile has the price but the product page doesn't. Do both emit a `product` **Record** and we merge?"
> **Domain expert:** "No merging. The **Follow Rule** runs per **Loop** node and passes the price as a **Variable**; only `product` declares the `product` **Record Type**, and its price **Field** reads that **Variable**."

## Flagged ambiguities

- "item" collides with Scrapy's `Item` and the `items:` config key — resolved: an emitted output object is a **Record**.
- "target" was used for both the whole website and individual URLs — resolved: the website is a **Site**; entry URLs are **Start Requests**.
