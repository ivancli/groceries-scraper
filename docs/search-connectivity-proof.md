# Private search route proof

Product Search will reach this scraper only through a private Workers VPC Service and a dedicated
outbound `cloudflared` connector in k3d
([technical plan](https://github.com/ivancli/groceries-tracker/blob/8202bfdcd7265b41fe34bdb042cebd44ae435d2a/docs/product-search-linking.md),
[search contract v1](https://github.com/ivancli/groceries-tracker/blob/8202bfdcd7265b41fe34bdb042cebd44ae435d2a/docs/product-search-contract.md)).
Before any real search is enabled, this runbook proves that route with harmless parts only
([#84](https://github.com/ivancli/groceries-scraper/issues/84)):

- `scrape proof-responder`: a fixture-only search v1 origin. It checks the origin credential,
  admits one execution at a time, flushes the `accepted` frame at once and holds the terminal
  frame for `--terminal-delay-ms`. Its `query` picks a fixture stream: `ok`, `fragmented`,
  `oversize`, `mismatched`, `duplicate`, `premature` or `late`. It makes no outbound request.
- [`deploy/k8s/overlays/local/search-proof`](../deploy/k8s/overlays/local/search-proof): the
  responder behind a ClusterIP Service, its own connector, and NetworkPolicies that allow only
  the connector in and nothing out.
- [`deploy/search-proof/worker`](../deploy/search-proof/worker): an isolated test Worker,
  never the tracker's, that reads each stream as the contract requires and reports timings.

The repository's tests prove both ends agree over loopback (`tests/test_search_proof_*.py`,
`node --test` in the Worker directory). **They are not evidence of the route.** Only the
evidence record below, filled in from a real run, is.

**A failed check blocks activation.** Record it and raise a new route decision. Never fall
back to a public or Access-protected hostname without that decision.

## Credentials

Three separate credentials, none shared:

| Credential | Lives in | Used by |
| --- | --- | --- |
| Tunnel token | Secret `scraper-search-connector` | the connector only |
| Origin credential | Secret `scraper-search-origin` and Worker secret `SCRAPER_ORIGIN_TOKEN` | the responder checks what the Worker sends |
| Proof token | Worker secret `PROOF_TOKEN` | whoever runs the proof |

The Dispatcher's `scrape-dispatcher` and `scrape-sinks` Secrets are never mounted here.

## Run the proof

Needs an account member with **Connectivity Directory Admin** (to create the VPC Service) and
Workers deploy access, Wrangler, and the k3d cluster from the README.

1. **Tunnel.** In Zero Trust, create a remotely managed Tunnel named `groceries-search-proof`.
   Add **no** public hostname and **no** private network route. Save its token to a file,
   not your shell history, and note the Tunnel ID.
2. **Secrets and workloads.**

   ```bash
   umask 077; secrets=~/.config/groceries-search-proof  # outside the checkout
   mkdir -p "$secrets" && openssl rand -hex 32 > "$secrets/origin-token"
   kubectl create secret generic scraper-search-origin --from-file=token="$secrets/origin-token"
   kubectl create secret generic scraper-search-connector --from-file=token="$secrets/tunnel-token"
   docker build -t groceries-scraper:dev . && k3d image import groceries-scraper:dev -c groceries
   kubectl apply -k deploy/k8s/overlays/local/search-proof
   kubectl logs deploy/scraper-search-connector | grep -i 'registered tunnel connection'
   ```

   The connector must register connections with `protocol=quic`. That is the evidence for
   outbound UDP 7844; it has no HTTP/2 fallback to hide behind.
3. **VPC Service.** Fill the Tunnel ID into
   [`deploy/search-proof/vpc-service.json`](../deploy/search-proof/vpc-service.json) and create
   it in the dashboard or with `wrangler vpc service create` (see `--help`; the CLI is beta).
   It targets only `scraper-search.default.svc.cluster.local:8080` over `http`.
4. **Test Worker.** Put the service ID into
   [`wrangler.jsonc`](../deploy/search-proof/worker/wrangler.jsonc), then from that directory:

   ```bash
   wrangler secret put SCRAPER_ORIGIN_TOKEN < ~/.config/groceries-search-proof/origin-token
   wrangler secret put PROOF_TOKEN
   wrangler deploy
   ```

5. **Run every scenario**, ten times for timings:

   ```bash
   curl -sS -X POST "$PROOF_URL/proof" -H "Authorization: Bearer $PROOF_TOKEN" -d '{}'
   ```

   `passed` must be true: `ok` and `fragmented` succeed with `acceptedMs` under 2000 and
   `terminalMs` at or past 3000 (the responder's delay), all inside 10 000; each faulty stream
   fails with its expected bounded code. Record the slowest `acceptedMs` of the ten.

## Checks

Run each from step 5's setup and record the outcome. Wait up to a minute after changing a
Secret: the kubelet refreshes mounted Secrets on its own sync.

| # | Check | How | Expected |
| --- | --- | --- | --- |
| C1 | Account access | step 3 and 4 succeed | VPC Service created and bound |
| C2 | QUIC over UDP 7844 | connector logs | connections registered with `protocol=quic` |
| C3 | Cluster DNS/ClusterIP routing | step 5 | every scenario as expected |
| C4 | No public route | Tunnel has no public hostname or network route; `kubectl get svc -A` | no NodePort/LoadBalancer, no `hostNetwork` pods for this proof |
| C5 | Wrong origin credential | `wrangler secret put SCRAPER_ORIGIN_TOKEN` with another value, run `ok` | `rejected`, 401 `unauthorized`; then restore |
| C6 | Missing origin credential | `kubectl exec deploy/scraper-search -- python -c "import urllib.request as u; u.urlopen(u.Request('http://127.0.0.1:8080/v1/search', b'{}'))"` | HTTP 401 `unauthorized` |
| C7 | Restricted ingress | `kubectl run probe --rm -it --image=curlimages/curl --restart=Never -- curl -m 5 http://scraper-search:8080/healthz` | times out |
| C8 | No egress from the responder | `kubectl exec deploy/scraper-search -- python -c "import socket; socket.create_connection(('1.1.1.1', 443), 5)"` | fails |
| C9 | Non-root, no stray mounts | `kubectl exec deploy/scraper-search -- id`; `… -- ls /var/run/secrets` | uid 10001; only `scraper-search`, no `kubernetes.io` token |
| C10 | Connector outage | `kubectl scale deploy/scraper-search-connector --replicas=0`, run `ok` | bounded failure within 10 s, recorded verbatim; passes again after scaling back |
| C11 | API outage | `kubectl scale deploy/scraper-search --replicas=0`, run `ok` | bounded failure within 10 s; passes after scaling back |
| C12 | Restart mid-search | start `ok`, then within 3 s `kubectl rollout restart deploy/scraper-search` | bounded failure (`disconnected` or `premature_eof`); responder logs show no second execution of that `request_id` |
| C13 | Connector restart mid-search | as C12 with `deploy/scraper-search-connector` | bounded failure; next run passes once it reconnects |
| C14 | Delay | `late` scenario | `admission_timeout` at about 2000 ms |
| C15 | Laptop offline/asleep | disconnect the network or sleep, run `ok` from elsewhere | bounded failure within 10 s; nothing replays on wake |
| C16 | Credential rotation | new `origin-token`, `kubectl create secret … --dry-run=client -o yaml \| kubectl apply -f -`, run `ok` | old Worker secret rejected (401); passes after updating the Worker secret; no restart needed |
| C17 | No retailer traffic | responder and connector logs throughout | only fixture requests; NetworkPolicy C8 holds |

## Evidence record

**Status: not run.** Fill in from a real run; leave nothing inferred.

| Field | Value |
| --- | --- |
| Date, operator | |
| Laptop network (home Wi-Fi, other) | |
| k3d/k3s version | |
| `cloudflared` image | `cloudflare/cloudflared:2026.10.0` (from the overlay) |
| Tunnel ID / VPC Service ID | |
| Test Worker version ID | |
| Proof responder image digest, `--terminal-delay-ms` | |
| `acceptedMs` over ten `ok` runs (min / median / max) | |
| `terminalMs` over ten `ok` runs (min / median / max) | |
| C1–C17 outcomes | |
| Failures and decisions raised | |

Afterwards: `kubectl delete -k deploy/k8s/overlays/local/search-proof`, delete both Secrets,
the test Worker, the VPC Service and the Tunnel unless the next ticket reuses them.
