"""`deploy/search-proof/setup.sh` run against fake CLIs: what it runs, and what it never leaks."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SETUP = ROOT / "deploy" / "search-proof" / "setup.sh"
WORKER = ROOT / "deploy" / "search-proof" / "worker"
SERVICE_ID = "01a12ddb-9346-7b50-b502-2c06fcab314a"
PROOF_URL = "https://groceries-search-proof.example.workers.dev"
TUNNEL_TOKEN = "tunnel-token-value"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="needs jq")

FAKES = {
    # Every fake logs its argv; the few that answer, answer like the real tool.
    "kubectl": """
case "$*" in
  *"--dry-run=client"*) echo "kind: Secret" ;;
  "apply -f -") cat >/dev/null; echo "secret/x configured" ;;
  logs*) echo "INF Registered tunnel connection protocol=${FAKE_PROTOCOL:-quic}" ;;
  "config current-context") echo k3d-groceries ;;
esac""",
    "npx": f"""
case "$*" in
  *"secret put"*) cat >/dev/null ;;
  *deploy*) echo "Deployed groceries-search-proof triggers"; echo "  {PROOF_URL}" ;;
esac""",
    "curl": 'echo \'{"passed":true,"runs":[{"scenario":"ok","passed":true}]}\'',
    "docker": "",
    "k3d": "",
}


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in FAKES.items():
        fake = bin_dir / name
        fake.write_text(f'#!/usr/bin/env bash\necho "{name} $*" >> "$FAKE_LOG"\n{body}\n')
        fake.chmod(0o755)
    token = tmp_path / "tunnel-token"
    token.write_text(TUNNEL_TOKEN)
    return {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_LOG": str(tmp_path / "calls.log"),
        "TUNNEL_TOKEN_FILE": str(token),
        "VPC_SERVICE_ID": SERVICE_ID,
        "SECRETS_DIR": str(tmp_path / "secrets"),
        "CF_API_TOKEN": "stale-token",
    }


@pytest.fixture(autouse=True)
def no_local_config() -> object:
    yield
    (WORKER / "wrangler.local.jsonc").unlink(missing_ok=True)


def run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SETUP), *args], env=env, capture_output=True, text=True, timeout=60
    )


def calls(env: dict[str, str]) -> list[str]:
    log = Path(env["FAKE_LOG"])
    return log.read_text().splitlines() if log.exists() else []


def test_help_names_the_two_inputs_from_the_dashboard(env: dict[str, str]) -> None:
    result = run(env, "--help")

    assert result.returncode == 0
    assert "TUNNEL_TOKEN_FILE" in result.stdout and "VPC_SERVICE_ID" in result.stdout
    assert calls(env) == []


@pytest.mark.parametrize("missing", ["TUNNEL_TOKEN_FILE", "VPC_SERVICE_ID"])
def test_a_missing_input_stops_before_touching_anything(env: dict[str, str], missing: str) -> None:
    del env[missing]

    result = run(env)

    assert result.returncode == 2
    assert missing in result.stderr
    assert calls(env) == []


def test_a_full_run_deploys_everything_and_passes_the_smoke_proof(env: dict[str, str]) -> None:
    tracked = (WORKER / "wrangler.jsonc").read_text()

    result = run(env)

    assert result.returncode == 0, result.stderr
    log = calls(env)
    sequence = [line.split()[0] + " " + line.split()[1] for line in log]
    assert sequence.index("kubectl apply") < sequence.index("kubectl rollout")
    assert "docker build" in sequence and "k3d image" in sequence
    assert any("apply -k deploy/k8s/overlays/local/search-proof" in line for line in log)
    # The Worker's real Service ID goes only into the ignored local config.
    assert (WORKER / "wrangler.jsonc").read_text() == tracked
    local = (WORKER / "wrangler.local.jsonc").read_text()
    assert SERVICE_ID in local and "REPLACE_WITH_VPC_SERVICE_ID" not in local
    wrangler = [line for line in log if line.startswith("npx wrangler")]
    assert all("--config wrangler.local.jsonc" in line for line in wrangler if "whoami" not in line)
    assert {"SCRAPER_ORIGIN_TOKEN", "PROOF_TOKEN"} <= {
        line.split("secret put ")[1].split()[0] for line in wrangler if "secret put" in line
    }
    assert PROOF_URL in result.stdout
    assert json.loads(result.stdout.strip().splitlines()[-1])["passed"] is True


def test_no_secret_reaches_a_command_line(env: dict[str, str]) -> None:
    run(env)

    secrets = [TUNNEL_TOKEN] + [p.read_text().strip() for p in Path(env["SECRETS_DIR"]).iterdir()]
    assert all(secret and secret not in line for secret in secrets for line in calls(env))


def test_generated_tokens_are_kept_across_reruns(env: dict[str, str]) -> None:
    run(env)
    first = {p.name: p.read_text() for p in Path(env["SECRETS_DIR"]).iterdir()}

    run(env)

    assert {p.name: p.read_text() for p in Path(env["SECRETS_DIR"]).iterdir()} == first
    assert all(oct(p.stat().st_mode)[-3:] == "600" for p in Path(env["SECRETS_DIR"]).iterdir())


def test_a_connector_without_quic_fails_the_setup(env: dict[str, str]) -> None:
    env["FAKE_PROTOCOL"] = "http2"
    env["QUIC_WAIT_SECONDS"] = "1"

    result = run(env)

    assert result.returncode == 1
    assert "QUIC" in result.stderr
    assert not any("wrangler deploy" in line for line in calls(env))


def test_skip_build_reuses_the_imported_image(env: dict[str, str]) -> None:
    run(env, "--skip-build")

    assert not any(line.startswith(("docker ", "k3d ")) for line in calls(env))


def test_a_stale_cf_api_token_is_ignored(env: dict[str, str]) -> None:
    result = run(env)

    assert "CF_API_TOKEN" in result.stderr  # says why it set it aside
