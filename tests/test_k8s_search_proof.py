"""The search-proof overlay: a fixture responder reachable only through a dedicated connector."""

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parent.parent
OVERLAY = ROOT / "deploy" / "k8s" / "overlays" / "local" / "search-proof"
WORKER = ROOT / "deploy" / "search-proof" / "worker"

pytestmark = pytest.mark.skipif(shutil.which("kubectl") is None, reason="needs kubectl")

Manifests = dict[tuple[str, str], dict[str, Any]]


@pytest.fixture(scope="module")
def docs() -> Manifests:
    built = subprocess.run(
        ["kubectl", "kustomize", str(OVERLAY)], check=True, capture_output=True, text=True
    ).stdout
    return {(doc["kind"], doc["metadata"]["name"]): doc for doc in yaml.safe_load_all(built)}


def pod(docs: Manifests, deployment: str) -> dict[str, Any]:
    spec: dict[str, Any] = docs[("Deployment", deployment)]["spec"]["template"]["spec"]
    return spec


def labels(docs: Manifests, deployment: str) -> dict[str, str]:
    template = docs[("Deployment", deployment)]["spec"]["template"]
    return template["metadata"]["labels"]  # type: ignore[no-any-return]


def test_the_test_worker_expects_the_responders_delay(docs: Manifests) -> None:
    wrangler = WORKER / "wrangler.jsonc"
    config = json.loads(re.sub(r"^\s*//.*$", "", wrangler.read_text(), flags=re.MULTILINE))
    (container,) = pod(docs, "scraper-search")["containers"]
    args = container["args"]

    assert config["vars"]["TERMINAL_DELAY_MS"] == args[args.index("--terminal-delay-ms") + 1]


def test_the_overlay_holds_only_the_proof_workloads(docs: Manifests) -> None:
    assert sorted(docs) == [
        ("Deployment", "scraper-search"),
        ("Deployment", "scraper-search-connector"),
        ("NetworkPolicy", "scraper-search"),
        ("NetworkPolicy", "scraper-search-connector"),
        ("Service", "scraper-search"),
        ("ServiceAccount", "scraper-search"),
        ("ServiceAccount", "scraper-search-connector"),
    ]


def test_the_api_is_a_cluster_ip_service_only(docs: Manifests) -> None:
    service = docs[("Service", "scraper-search")]["spec"]
    assert service.get("type", "ClusterIP") == "ClusterIP"
    assert service["ports"] == [{"name": "http", "port": 8080, "targetPort": "http"}]
    assert service["selector"].items() <= labels(docs, "scraper-search").items()
    assert not any(kind in {"Ingress", "IngressRoute"} for kind, _ in docs)


def test_the_responder_is_one_recreated_fixture_replica(docs: Manifests) -> None:
    deployment = docs[("Deployment", "scraper-search")]["spec"]
    assert deployment["replicas"] == 1
    assert deployment["strategy"] == {"type": "Recreate"}
    (container,) = pod(docs, "scraper-search")["containers"]
    assert container["image"] == "groceries-scraper:dev"
    assert container["args"] == [
        "proof-responder",
        "--token-file",
        "/var/run/secrets/scraper-search/token",
        "--port",
        "8080",
        "--terminal-delay-ms",
        "3000",  # well past the two-second admission budget, inside ten seconds
    ]
    assert container["ports"] == [{"name": "http", "containerPort": 8080}]
    assert container["readinessProbe"]["httpGet"] == {"path": "/healthz", "port": "http"}


def test_the_connector_runs_a_vpc_capable_cloudflared_over_quic_only(docs: Manifests) -> None:
    (container,) = pod(docs, "scraper-search-connector")["containers"]
    image, tag = container["image"].split(":")
    assert image == "cloudflare/cloudflared"
    assert tuple(map(int, tag.split("."))) >= (2025, 7, 0)
    args = container["args"]
    assert args[args.index("--protocol") + 1] == "quic"  # never a silent HTTP/2 fallback
    assert "--no-autoupdate" in args
    assert args[args.index("--token-file") + 1] == "/etc/cloudflared/token"
    assert not any(re.search(r"--token(=|$)", arg) for arg in args)  # never on the command line
    assert "--url" not in args and "--hostname" not in args  # no published ingress


@pytest.mark.parametrize("deployment", ["scraper-search", "scraper-search-connector"])
def test_workloads_run_non_root_without_api_tokens_or_host_access(
    docs: Manifests, deployment: str
) -> None:
    spec = pod(docs, deployment)
    assert spec["serviceAccountName"] == deployment
    assert spec["automountServiceAccountToken"] is False
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert spec["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    for host in ("hostNetwork", "hostPID", "hostIPC"):
        assert not spec.get(host)
    assert all("hostPath" not in volume for volume in spec["volumes"])
    (container,) = spec["containers"]
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    assert "hostPort" not in str(container.get("ports", []))
    assert "envFrom" not in container  # no sink, Dispatcher or personal credentials


def test_each_workload_mounts_only_its_own_credential(docs: Manifests) -> None:
    def secrets(deployment: str) -> set[str]:
        return {
            volume["secret"]["secretName"]
            for volume in pod(docs, deployment)["volumes"]
            if "secret" in volume
        }

    assert secrets("scraper-search") == {"scraper-search-origin"}
    assert secrets("scraper-search-connector") == {"scraper-search-connector"}


def test_only_the_connector_may_reach_the_api(docs: Manifests) -> None:
    policy = docs[("NetworkPolicy", "scraper-search")]["spec"]
    assert policy["podSelector"]["matchLabels"].items() <= labels(docs, "scraper-search").items()
    assert sorted(policy["policyTypes"]) == ["Egress", "Ingress"]
    (ingress,) = policy["ingress"]
    (peer,) = ingress["from"]
    assert peer == {
        "podSelector": {"matchLabels": {"app.kubernetes.io/name": "scraper-search-connector"}}
    }
    assert peer["podSelector"]["matchLabels"].items() <= (
        labels(docs, "scraper-search-connector").items()
    )
    assert ingress["ports"] == [{"protocol": "TCP", "port": 8080}]
    assert policy["egress"] == []  # a fixture responder contacts nothing: no retailer, no LAN


def test_the_connector_may_reach_only_dns_the_tunnel_over_udp_and_the_api(docs: Manifests) -> None:
    policy = docs[("NetworkPolicy", "scraper-search-connector")]["spec"]
    assert sorted(policy["policyTypes"]) == ["Egress", "Ingress"]
    assert policy["ingress"] == []
    rules = {
        tuple((port["protocol"], port["port"]) for port in rule["ports"]): rule.get("to")
        for rule in policy["egress"]
    }
    assert set(rules) == {
        (("UDP", 53), ("TCP", 53)),
        (("UDP", 7844),),  # QUIC only: TCP 7844 would only serve the HTTP/2 fallback
        (("TCP", 8080),),
    }
    dns = rules[(("UDP", 53), ("TCP", 53))]
    assert dns == [
        {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
            "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
        }
    ]
    assert rules[(("TCP", 8080),)] == [
        {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "scraper-search"}}}
    ]
    # The tunnel's edge addresses are Cloudflare's; private ranges, i.e. the home LAN, stay shut.
    (tunnel,) = rules[(("UDP", 7844),)]
    assert tunnel["ipBlock"]["cidr"] == "0.0.0.0/0"
    assert set(tunnel["ipBlock"]["except"]) == {
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "100.64.0.0/10",
    }
