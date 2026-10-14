# Disabled Coles discovery adapter

Issue [#87](https://github.com/ivancli/groceries-scraper/issues/87), implementing the approved
[search contract](https://github.com/ivancli/groceries-tracker/blob/8202bfdcd7265b41fe34bdb042cebd44ae435d2a/docs/product-search-contract.md).
The adapter is **disabled**. Fixture extraction is independent of the supervised runner
and service work; it does not establish live launch readiness.

## Public boundary

[`search/coles.py`](../src/groceries_scraper/search/coles.py) exposes:

- `capability()`: the reviewed `coles-live-v1` definition, with `enabled: false`, fixed
  product/image rules and no Home Store mappings. It is separate from price `sites/`.
- `build_request(query)`: the exact query encoded into the retained entry route
  `https://www.coles.com.au/search?q=…`, the existing configured User-Agent, browser
  rendering and `obey_robots: true`. It constructs a request description without I/O.
- `parse_response(request, body, status=…, response_url=…)`: replay or live response bytes
  into a `Discovery` containing `status`, `results`, `context` and a safe error code.
  HTTP refusal/challenge is `blocked`; unrecognized or corrupt discovery is `failed`.

The intended supervised service must check the disabled capability before starting work,
apply aggregate retailer permits to every actual request/resource/redirect, enforce robots,
the deadline and cancellation, and call this projector on the final document. It must not
infer success from zero generic Records or bypass robots via the entry route's redirect.
This module starts no browser, publishes no manifest and uses no Sink, Dispatcher or price
configuration. Service registration and authenticated publication belong to
[#88](https://github.com/ivancli/groceries-scraper/issues/88), after the runner foundation.
No request, result or context option accepts a browser-supplied origin or configuration.

## Projection and validation

The embedded `searchResults` must match the exact submitted query and first page, contain
valid counts and a result array, and not report alternate results. Its order is authoritative:
retain `PRODUCT` entries, exclude explicit `adId`/`adSource`/`featured` advertising and
visible Sponsored labels, and exclude `PRODUCT_ASSOCIATION` recommendations. This preserves
the left primary products in association wrappers. Literal title containment is not a
membership rule. Validate every eligible primary product, deduplicate by its identity/URL,
then take ten. Corruption beyond the first ten still fails the response.

Retain retailer IDs as strings and join them to DOM-issued product links by the observed
numeric suffix. Never construct a slug from a name or treat conflicting URLs as equivalent.
Titles are the rendered tile titles, with whitespace collapsed, limited to 512 code points;
optional name/brand/size fields are limited to 200. Invalid required fields fail the complete
discovery, rather than being dropped into a misleading empty success.
The projected context/results payload is capped at 60 KiB, leaving room inside the 64 KiB
terminal-frame ceiling for service identity and source-time fields.

Product links allow HTTPS `www.coles.com.au/product/` only. Images allow HTTPS
`cdn.productimages.coles.com.au/productimages/`, or the exact retailer `/_next/image`
optimizer with exactly one absolute HTTPS `url` source matching that CDN rule. Both reject
credentials, nondefault ports, fragments, whitespace, backslashes, dot traversal and encoded
path separators/dots. Relative DOM URLs resolve against the fixed retailer origin. An invalid
or missing image becomes null; a later image-load failure must render a selectable placeholder
in the picker. Images are not fetched by this adapter.

## Context

No reliable Home Store application has been demonstrated. Queries describe the unauthenticated
online delivery default with `home_store_applied: false`, location `default`, and a label that
explicitly says its location is unverified. The retained browser trial displayed delivery to
Melbourne 3000; that notice is absent from the initial HTTP document. The adapter therefore
does not assert that every future document is for Melbourne, or claim Home Store stock/prices.
Verifying the deployed browser's actual default context remains a launch gate. If verified
context changes the projection or label, update the definition/version before enablement.

## Evidence and unresolved gates

[`tests/fixtures/coles-search/milk.html`](../tests/fixtures/coles-search/milk.html) is an
allowlisted derivative of the genuine configured-User-Agent response retained by the tracker
research. Its source SHA-256 is
`41f360c8bcd06b7ffae0d60f7d3c4b517d26e3ef6a7fc1b6d6d03a4b1d0d062c`, 639,381 bytes,
collected 10 October 2026. It retains 53 ordered entries: 50 primary products, three
association recommendations, and three sponsored primary products. Projection yields 47
eligible candidates before the ten-result limit. Metadata and derivation are in
[`milk.meta.json`](../tests/fixtures/coles-search/milk.meta.json).

The fixture includes only reviewed membership, name/brand/size and DOM URL/title/image fields.
Opaque advertising values become presence markers. It omits search-session IDs, cookies,
profiles, request/response headers, prices, customer state and unrelated scripts. The reduced
challenge fixture preserves the observed Incapsula iframe shape without identifying query
parameters. Tests construct mutations for duplication, corruption, refinements, optimizer
validation and empty-response consistency. **Simulated refined and empty responses are not
genuine retailer evidence.** Existing price-page captures are not used for discovery proof.

On 12 October 2026, after inspecting the retained evidence, one configured-User-Agent GET
of `https://www.coles.com.au/robots.txt` returned HTTP 200. The retained public
[`robots.txt`](../tests/fixtures/coles-search/robots.txt) disallows `/search/`, including the
observed `/search/products` redirect destination. The current `obey_robots: true` policy
therefore prevents new live queries. No search, alternate route, challenge retry or image
request followed that check. Historical browser access does not override this policy.

Issue #87's genuine-empty and representative refined-query captures remain outstanding.
Resolve the access-policy conflict before new requests; do not silently change the policy,
identity, transport or introduce a catalogue fallback. Keep the adapter disabled until those
fixtures, actual context, service/integration tests, title/image/placeholder rendering,
stop-on-block behavior and repeated cold/warm end-to-end timing pass the retained launch gates.
Production activation remains [#89](https://github.com/ivancli/groceries-scraper/issues/89).

Replay checks: `uv run pytest tests/test_coles_search.py`. They require no retailer access and
produce no prices, stock, Last Checked, Watch List demand or alerts.
