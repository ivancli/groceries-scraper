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

## Set up or move the route

Two dashboard steps, then one command. Repeat both for every new environment (another
machine or cluster): a VPC Service is tied to one tunnel, and two connectors sharing a tunnel
token would split traffic between clusters. To move rather than add, stop the old connector
first and reuse its tunnel and VPC Service.

| Piece | Once | Per environment |
| --- | --- | --- |
| Account roles (**Connectivity Directory Admin**), `npx wrangler login` | yes | |
| Test Worker | yes | redeployed by `setup.sh` with the new Service ID |
| Tunnel, VPC Service | | dashboard, below |
| Tokens, k8s Secrets, image, overlay, QUIC check, Worker secrets | | `setup.sh` |
| Checks C1–C17 | yes, for the route decision | a passing smoke proof (`setup.sh`) |

1. **Tunnel.** Dashboard → Workers VPC → **Tunnels** → **Create**, a remotely managed tunnel
   with **no** public hostname and **no** route. Copy only its token into a file, not your
   shell history:

   ```bash
   mkdir -p ~/.config/groceries-search-proof
   read -rsp 'Tunnel token: ' t && printf '%s' "$t" > ~/.config/groceries-search-proof/tunnel-token && unset t
   ```

2. **VPC Service.** Workers VPC → **VPC Services** → **Create** with the values in
   [`vpc-service.json`](../deploy/search-proof/vpc-service.json): that tunnel, host
   `scraper-search.default.svc.cluster.local`, HTTP port `8080`, tunnel as resolver. Note its
   Service ID.
3. **Everything else**, against the current kubectl context:

   ```bash
   TUNNEL_TOKEN_FILE=~/.config/groceries-search-proof/tunnel-token \
   VPC_SERVICE_ID=<service id> \
   deploy/search-proof/setup.sh
   ```

   It keeps the origin and proof tokens in `~/.config/groceries-search-proof` (generated
   once), applies both Secrets, builds and imports the image (`--skip-build` reuses it),
   applies the overlay, and fails unless the connector registers over QUIC. It then deploys
   the test Worker from an ignored `wrangler.local.jsonc` that holds the real Service ID, sets
   its secrets, and ends with one passing smoke proof and the `PROOF_URL` to export. Rerun it
   after rotating any token; it is safe to repeat. A failed QUIC registration blocks
   activation: don't widen the connector's NetworkPolicy or change `--protocol` to pass it.

## Run the proof

With `PROOF_URL` and `PROOF_TOKEN` exported as `setup.sh` prints, run every scenario ten
times for timings:

```bash
curl -sS -X POST "$PROOF_URL/proof" -H "Authorization: Bearer $PROOF_TOKEN" -d '{}'
```

`passed` must be true: `ok` and `fragmented` succeed with `acceptedMs` under 2000 and the
result arriving at least the responder's 3000 ms delay later (`TERMINAL_DELAY_MS`), all
inside 10 000; each faulty stream fails with its expected bounded code. A route that
buffered the stream would deliver both frames together and fail. Record the slowest
`acceptedMs` of the ten.

## Checks

Run each from a passing setup and record the outcome. Wait up to a minute after changing a
Secret: the kubelet refreshes mounted Secrets on its own sync.

| # | Check | How | Expected |
| --- | --- | --- | --- |
| C1 | Account access | the VPC Service is created and `setup.sh` deploys the Worker | VPC Service created and bound |
| C2 | QUIC over UDP 7844 | connector logs | connections registered with `protocol=quic` |
| C3 | Cluster DNS/ClusterIP routing | the ten runs above | every scenario as expected |
| C4 | No public route | Tunnel has no public hostname or network route; `kubectl get svc -A`; `kubectl get pods -l app.kubernetes.io/part-of=groceries-scraper -o jsonpath='{..hostNetwork}'` | no NodePort/LoadBalancer; empty `hostNetwork` output |
| C5 | Wrong origin credential | `wrangler secret put SCRAPER_ORIGIN_TOKEN` with another value, run `ok` | `rejected`, 401 `unauthorized`; then restore |
| C6 | Missing origin credential (from inside the pod: the Worker always sends one) | `kubectl exec deploy/scraper-search -- python -c "import urllib.request as u; u.urlopen(u.Request('http://127.0.0.1:8080/v1/search', b'{}'))"` | HTTP 401 `unauthorized` |
| C7 | Restricted ingress | `kubectl run probe --rm -it --image=curlimages/curl --restart=Never -- curl -m 5 http://scraper-search:8080/healthz` | refused at once or times out (k3s rejects rather than drops); the API still answers inside its own pod |
| C8 | No egress from the responder | `kubectl exec deploy/scraper-search -- python -c "import socket; socket.create_connection(('1.1.1.1', 443), 5)"` | refused or times out, while an unrestricted pod can reach the same address |
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

**Status: passed, 2026-10-12.** Every check passed over the real private route. This proves
the route with fixtures only; it says nothing about retailer timing, which #89 measures.

| Field | Value |
| --- | --- |
| Date, operator | 2026-10-12, Ivan Li |
| Laptop OS, sleep settings, network (home Wi-Fi, other) | WSL2 (Linux 6.6.87.2-microsoft-standard-WSL2); sleep settings and network not recorded |
| Worker colo (`cf-ray` suffix of the proof response) | SYD, all ten runs |
| k3d/k3s version | k3d v5.9.0, k3s v1.35.5+k3s1 |
| `cloudflared` image | `cloudflare/cloudflared:2026.10.0`, reports Version 2026.10.0 |
| Tunnel ID / VPC Service ID | `646ba327-4365-4829-9625-10a09b994d15` / `01a12ddb-9346-7b50-b502-2c06fcab314a` |
| Test Worker version ID | `03f75faa-73ad-4a20-8ef0-26577389a5f3` (after the C16b redeploy) |
| Proof responder image digest, `--terminal-delay-ms` | `sha256:e8ad31a4a08b31f5f19b9b34bef06d68f81720614c00a57aabd290a98cc139fa`, 3000 |
| `acceptedMs` over ten `ok` runs (min / median / max) | 36 / 45.5 / 149 |
| `terminalMs` over ten `ok` runs (min / median / max) | 3041 / 3047.5 / 3156 |
| Failures and decisions raised | none |

| Check | Outcome |
| --- | --- |
| C1 Account access | Pass: VPC Service created, test Worker deployed with the binding, ten proof runs succeeded |
| C2 QUIC over UDP 7844 | Pass: four connections registered `protocol=quic` (syd06, syd08) |
| C3 Cluster DNS/ClusterIP routing | Pass: ten of ten runs `passed: true`; every scenario its expected outcome in all ten |
| C4 No public route | Pass: tunnel has no public hostname or route (dashboard, confirmed by the operator); `scraper-search` is `ClusterIP`; neither pod uses `hostNetwork` |
| C5 Wrong origin credential | Pass: `rejected`, 401 `unauthorized`; passed again after restoring it |
| C6 Missing origin credential | Pass: in-pod request without a credential got `HTTPError 401` `unauthorized` |
| C7 Restricted ingress | Pass: a probe pod was refused at once (curl exit 7); the API answers inside its own pod |
| C8 No egress from the responder | Pass: connection to 1.1.1.1:443 refused at once; an unrestricted pod reached it (HTTP 301) |
| C9 Non-root, no stray mounts | Pass: `uid=10001`; `/var/run/secrets` holds only `scraper-search`; connector uid 65532; no service-account token; each pod mounts only its own Secret (+ `/tmp` emptyDir) |
| C10 Connector outage | Pass: `unreachable` after 12 ms; passed after scaling back |
| C11 API outage | Pass: `unreachable` after 57 ms; passed after scaling back |
| C12 API restart mid-search | Pass: a rollout restart drained the search (the responder, as PID 1, ignores SIGTERM, so the pod waits out its 30 s grace); a forced kill failed it `disconnected` at 3131 ms; the new pod logged no replay |
| C13 Connector restart mid-search | Pass: rollout restart and force-delete both drained it; SIGKILL of `cloudflared` failed it `disconnected` at 6040 ms; the next run passed |
| C14 Delay | Pass: `late` failed `admission_timeout` at 2000 ms in all ten runs |
| C15 Laptop offline or asleep | Pass: bounded failure while offline and recovery afterwards, confirmed by the operator; timings not recorded |
| C16 Origin credential rotation | Pass: rotated Secret took effect after 9 s with no pod restart; 401 until the Worker secret was updated, then passed |
| C16b Tunnel and proof token rotation | Pass: old proof token 401, new one passes; tunnel token refreshed and applied with `setup.sh`, smoke proof passed (operator) |
| C17 No retailer traffic | Pass: responder log holds only `POST /v1/search` and `GET /healthz`; C8 holds |

Follow-up for #88: the real service should handle SIGTERM, so a restart doesn't wait out the
full grace period.

Known limit: the responder's execution slot is per process, and `Recreate` is what stops two
pods overlapping. That is enough for fixtures; the real service needs the authoritative
admission the contract requires.

Afterwards: `kubectl delete -k deploy/k8s/overlays/local/search-proof`, delete both Secrets,
the test Worker, the VPC Service and the Tunnel unless the next ticket reuses them.
