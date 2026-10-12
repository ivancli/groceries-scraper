#!/usr/bin/env bash
# Sets up, or moves, the private search route proof after its two dashboard steps.
# Safe to rerun: tokens are kept, Secrets and workloads are applied, the Worker redeployed.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: TUNNEL_TOKEN_FILE=<file> VPC_SERVICE_ID=<id> deploy/search-proof/setup.sh [--skip-build]

First, once per environment, in the Cloudflare dashboard (Workers VPC):
  1. Tunnels -> Create: a remotely managed tunnel, no public hostname, no routes.
     Save its token to a file (TUNNEL_TOKEN_FILE).
  2. VPC Services -> Create: that tunnel, host scraper-search.default.svc.cluster.local,
     HTTP port 8080, tunnel as resolver. Note its Service ID (VPC_SERVICE_ID).

Then this script, from any directory, against the current kubectl context:
  - generates the origin and proof tokens once, in SECRETS_DIR
  - applies both k8s Secrets, builds and imports the image, applies the overlay
  - waits for the connector to register over QUIC (fails if it can't)
  - deploys the test Worker with that Service ID and sets its secrets
  - runs one smoke proof and prints how to run the full checks

Environment:
  TUNNEL_TOKEN_FILE   required: the tunnel token, never passed on a command line
  VPC_SERVICE_ID      required: the VPC Service the Worker binds
  SECRETS_DIR         default ~/.config/groceries-search-proof
  K3D_CLUSTER         default groceries
  QUIC_WAIT_SECONDS   default 90
  --skip-build        reuse the groceries-scraper:dev image already in the cluster
EOF
}

skip_build=false
for arg in "$@"; do
  case "$arg" in
    -h | --help) usage; exit 0 ;;
    --skip-build) skip_build=true ;;
    *) echo "Unknown argument: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

for name in TUNNEL_TOKEN_FILE VPC_SERVICE_ID; do
  if [[ -z "${!name:-}" ]]; then
    echo "$name is required; see --help for the dashboard steps that give it." >&2
    exit 2
  fi
done
if [[ ! -s "$TUNNEL_TOKEN_FILE" ]]; then
  echo "TUNNEL_TOKEN_FILE $TUNNEL_TOKEN_FILE is missing or empty." >&2
  exit 2
fi

secrets="${SECRETS_DIR:-$HOME/.config/groceries-search-proof}"
cluster="${K3D_CLUSTER:-groceries}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
worker="$root/deploy/search-proof/worker"
cd "$root"

step() { printf '\n==> %s\n' "$*" >&2; }

if [[ -n "${CF_API_TOKEN:-}" ]]; then
  # Wrangler prefers this deprecated variable over your login, and a stale one fails with 9109.
  echo "Ignoring CF_API_TOKEN for this run; Wrangler uses your login or CLOUDFLARE_API_TOKEN." >&2
  unset CF_API_TOKEN
fi

step "Cluster $(kubectl config current-context); Cloudflare account"
npx wrangler whoami >&2

step "Tokens in $secrets (generated once, kept on rerun)"
mkdir -p "$secrets" && chmod 700 "$secrets"
for token in origin-token proof-token; do
  [[ -s "$secrets/$token" ]] || (umask 077 && openssl rand -hex 32 > "$secrets/$token")
  chmod 600 "$secrets/$token"
done

apply_secret() {  # prints kubectl's verdict: created, configured or unchanged
  kubectl create secret generic "$1" --from-file=token="$2" --dry-run=client -o yaml \
    | kubectl apply -f -
}

step "Kubernetes Secrets"
apply_secret scraper-search-origin "$secrets/origin-token" >&2
connector_secret="$(apply_secret scraper-search-connector "$TUNNEL_TOKEN_FILE")"
echo "$connector_secret" >&2

if [[ "$skip_build" == false ]]; then
  step "Image groceries-scraper:dev into k3d cluster $cluster"
  docker build -t groceries-scraper:dev . >&2
  k3d image import groceries-scraper:dev -c "$cluster" >&2
fi

step "Responder and connector"
kubectl apply -k deploy/k8s/overlays/local/search-proof >&2
if [[ "$connector_secret" == *configured* ]]; then
  # A running connector keeps the token it started with.
  kubectl rollout restart deploy/scraper-search-connector >&2
fi
for deployment in scraper-search scraper-search-connector; do
  kubectl rollout status "deploy/$deployment" --timeout=3m >&2
done

step "Connector registration over QUIC"
deadline=$((SECONDS + ${QUIC_WAIT_SECONDS:-90}))
until kubectl logs deploy/scraper-search-connector 2>/dev/null \
  | grep -q 'Registered tunnel connection.*protocol=quic'; do
  if ((SECONDS >= deadline)); then
    echo "The connector never registered over QUIC: outbound UDP 7844 may be blocked." >&2
    echo "Don't widen its NetworkPolicy or change --protocol; that fails check C2." >&2
    exit 1
  fi
  sleep 3
done
echo "QUIC registered." >&2

step "Test Worker bound to VPC Service $VPC_SERVICE_ID"
cd "$worker"
# The tracked config keeps its placeholder; the real ID lives only in this ignored copy.
sed "s/REPLACE_WITH_VPC_SERVICE_ID/$VPC_SERVICE_ID/" wrangler.jsonc > wrangler.local.jsonc
deployed="$(npx wrangler deploy --config wrangler.local.jsonc)"
echo "$deployed" >&2
proof_url="$(grep -oE 'https://[A-Za-z0-9.-]+\.workers\.dev' <<<"$deployed" | head -1)"
if [[ -z "$proof_url" ]]; then
  echo "Couldn't find the Worker's workers.dev URL in the deploy output." >&2
  exit 1
fi
npx wrangler secret put SCRAPER_ORIGIN_TOKEN --config wrangler.local.jsonc < "$secrets/origin-token" >&2
npx wrangler secret put PROOF_TOKEN --config wrangler.local.jsonc < "$secrets/proof-token" >&2

step "Smoke proof"
for attempt in 1 2 3 4 5 6; do
  # The header comes from a file descriptor, so the proof token never shows in `ps`.
  report="$(curl -sS -X POST "$proof_url/proof" \
    -H @<(printf 'Authorization: Bearer %s\n' "$(cat "$secrets/proof-token")") \
    -d '{"scenarios":["ok"]}' || true)"
  if jq -e '.passed == true' <<<"$report" >/dev/null 2>&1; then
    cat >&2 <<EOF

Route is up. For the full checks (docs/search-connectivity-proof.md):
  export PROOF_URL=$proof_url
  export PROOF_TOKEN=\$(cat $secrets/proof-token)
EOF
    echo "$proof_url"
    jq -c . <<<"$report"
    exit 0
  fi
  echo "Attempt $attempt: ${report:-no response}" >&2
  sleep 5  # new Worker secrets can take a few seconds to apply
done
echo "The smoke proof never passed; see the last report above." >&2
exit 1
