# Pure extraction engine behind a thin Scrapy adapter

The extraction engine — turning `(response body, content type, Page Type, Variables)` into **Records**, Follow Requests and an **Extraction Trace** — is a pure Python module with no Scrapy or Twisted imports. Scrapy is used only as a transport adapter: one generic spider that dispatches every response to the engine, the capture/replay downloader middleware, and a Record-writing pipeline.

We chose this over the obvious path (Scrapy `ItemLoader`s, one callback per Page Type, logic inside the spider) because **Replay**, Golden Fixture tests and `scrape inspect` all need to run extraction over recorded **Captures** without a live crawl, and because unit-testing extraction logic inside Twisted's reactor is slow and awkward.

## Consequences

- Do not import `scrapy` from the engine package; the engine's input is plain bytes + metadata, its output plain dataclasses. Scrapy `Selector` (parsel) is allowed since it is a standalone library.
- The engine never performs I/O; it *describes* requests (Request Templates rendered to method/URL/headers/body) and the adapter issues them.
- Swapping transport later (Playwright for `render: browser`, or `httpx`) means writing a new adapter, not touching extraction.
