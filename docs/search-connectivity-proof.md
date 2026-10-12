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
   outbound UDP 7844; it has no HTTP/2 fallback to hide behind. If it can't register, that
   blocks activation: don't widen its NetworkPolicy or switch protocol to make it pass.
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

   `passed` must be true: `ok` and `fragmented` succeed with `acceptedMs` under 2000 and the
   result arriving at least the responder's 3000 ms delay later (`TERMINAL_DELAY_MS`), all
   inside 10 000; each faulty stream fails with its expected bounded code. A route that
   buffered the stream would deliver both frames together and fail. Record the slowest `acceptedMs` of the ten.

## Checks

Run each from step 5's setup and record the outcome. Wait up to a minute after changing a
Secret: the kubelet refreshes mounted Secrets on its own sync.

| # | Check | How | Expected |
| --- | --- | --- | --- |
| C1 | Account access | step 3 and 4 succeed | VPC Service created and bound |
| C2 | QUIC over UDP 7844 | connector logs | connections registered with `protocol=quic` |
| C3 | Cluster DNS/ClusterIP routing | step 5 | every scenario as expected |
| C4 | No public route | Tunnel has no public hostname or network route; `kubectl get svc -A`; `kubectl get pods -l 'app.kubernetes.io/name in (scraper-search,scraper-search-connector)' -o jsonpath='{..hostNetwork}'` (pod templates carry only the name label) | no NodePort/LoadBalancer; two pods matched, empty `hostNetwork` output |
| C5 | Wrong origin credential | `wrangler secret put SCRAPER_ORIGIN_TOKEN` with another value, run `ok` | `rejected`, 401 `unauthorized`; then restore |
| C6 | Missing origin credential (from inside the pod: the Worker always sends one) | `kubectl exec deploy/scraper-search -- python -c "import urllib.request as u; u.urlopen(u.Request('http://127.0.0.1:8080/v1/search', b'{}'))"` | HTTP 401 `unauthorized` |
| C7 | Restricted ingress | `kubectl run probe --rm -it --image=curlimages/curl --restart=Never -- curl -m 5 http://scraper-search:8080/healthz` | times out |
| C8 | No egress from the responder | `kubectl exec deploy/scraper-search -- python -c "import socket; socket.create_connection(('1.1.1.1', 443), 5)"` | fails |
| C9 | Non-root, no stray mounts | `kubectl exec deploy/scraper-search -- id`; `… -- ls /var/run/secrets` | uid 10001; only `scraper-search`, no `kubernetes.io` token |
| C10 | Connector outage | `kubectl scale deploy/scraper-search-connector --replicas=0`, run `ok` | bounded failure within 10 s, recorded verbatim; passes again after scaling back |
| C11 | API outage | `kubectl scale deploy/scraper-search --replicas=0`, run `ok` | bounded failure within 10 s; passes after scaling back |
| C12 | Restart mid-search | start `ok`, then within 3 s `kubectl rollout restart deploy/scraper-search` | bounded failure (`disconnected` or `premature_eof`); responder logs show no second execution of that `request_id` |
| C13 | Connector restart mid-search | as C12 with `deploy/scraper-search-connector` | bounded failure; next run passes once it reconnects |
| C14 | Delay | `late` scenario | `admission_timeout` at about 2000 ms |
| C15 | Laptop offline/asleep | disconnect the network or sleep, run `ok` from elsewhere | bounded failure within 10 s; nothing replays on wake |
| C16 | Origin credential rotation | new `origin-token`, `kubectl create secret … --dry-run=client -o yaml \| kubectl apply -f -`, run `ok` | old Worker secret rejected (401); passes after updating the Worker secret; no restart needed |
| C16b | Tunnel token and proof token rotation | refresh the Tunnel's token, update `scraper-search-connector`, restart the connector; `wrangler secret put PROOF_TOKEN` | old tokens stop working; the proof passes with the new ones |
| C17 | No retailer traffic | responder and connector logs throughout | only fixture requests; NetworkPolicy C8 holds |

## Evidence record

**Status: partial, 2026-10-12.** The route proof (step 5) passed; checks with no recorded
outcome below are not run. Activation stays blocked until every check passes.

| Field | Value |
| --- | --- |
| Date, operator | 2026-10-12, Ivan Li |
| Laptop OS, sleep settings, network (home Wi-Fi, other) | WSL2 (Linux 6.6.87.2-microsoft-standard-WSL2); sleep settings and network not recorded |
| Worker colo (`cf-ray` suffix of the proof response) | SYD, all ten runs |
| k3d/k3s version | k3d v5.9.0, k3s v1.35.5+k3s1 |
| `cloudflared` image | `cloudflare/cloudflared:2026.10.0`, reports Version 2026.10.0 |
| Tunnel ID / VPC Service ID | `646ba327-4365-4829-9625-10a09b994d15` / `01a12ddb-9346-7b50-b502-2c06fcab314a` |
| Test Worker version ID | not recorded |
| Proof responder image digest, `--terminal-delay-ms` | `sha256:e8ad31a4a08b31f5f19b9b34bef06d68f81720614c00a57aabd290a98cc139fa`, 3000 |
| `acceptedMs` over ten `ok` runs (min / median / max) | 36 / 45.5 / 149 |
| `terminalMs` over ten `ok` runs (min / median / max) | 3041 / 3047.5 / 3156 |
| Failures and decisions raised | none |

| Check | Outcome |
| --- | --- |
| C1 Account access | Pass: VPC Service created, test Worker deployed with the binding, ten proof runs succeeded |
| C2 QUIC over UDP 7844 | Pass: four connections registered `protocol=quic` (syd06, syd08) |
| C3 Cluster DNS/ClusterIP routing | Pass: ten of ten runs `passed: true`; every scenario its expected outcome in all ten |
| C4 No public route | Partial: `scraper-search` is `ClusterIP`; neither pod uses `hostNetwork`. Tunnel hostname/route check in the dashboard not recorded |
| C9 Non-root, no stray mounts | Partial: uid 10001 and 65532, `automountServiceAccountToken: false`, each pod mounts only its own Secret (+ `/tmp` emptyDir). In-pod `id`/`ls` not run |
| C14 Delay | Pass: `late` failed `admission_timeout` at 2000 ms in all ten runs |
| C17 No retailer traffic | Partial: responder log holds only `POST /v1/search` (99) and `GET /healthz` (381); C8 not run |
| C5–C8, C10–C13, C15, C16, C16b | Not run |

Known limit: the responder's execution slot is per process, and `Recreate` is what stops two
pods overlapping. That is enough for fixtures; the real service needs the authoritative
admission the contract requires.

Afterwards: `kubectl delete -k deploy/k8s/overlays/local/search-proof`, delete both Secrets,
the test Worker, the VPC Service and the Tunnel unless the next ticket reuses them.
