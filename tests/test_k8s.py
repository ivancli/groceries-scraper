import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from groceries_scraper.cli import app

ROOT = Path(__file__).parent.parent
LOCAL = ROOT / "deploy" / "k8s" / "overlays" / "local"

pytestmark = pytest.mark.skipif(shutil.which("kubectl") is None, reason="needs kubectl")


def _build(overlay: Path) -> str:
    return subprocess.run(
        ["kubectl", "kustomize", str(overlay)], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture(scope="module")
def local_job() -> dict[str, Any]:
    (job,) = yaml.safe_load_all(_build(LOCAL))
    assert isinstance(job, dict)
    return job


def test_local_job_follows_run_health(local_job: dict[str, Any]) -> None:
    spec = local_job["spec"]
    assert spec["backoffLimit"] == 2
    assert spec["activeDeadlineSeconds"] == 14400
    assert spec["ttlSecondsAfterFinished"] == 604800
    # Rule order matters: an evicted pod must not fail the Job even if it exited 1–4.
    assert spec["podFailurePolicy"]["rules"] == [
        {"action": "Ignore", "onPodConditions": [{"type": "DisruptionTarget"}]},
        {
            "action": "FailJob",
            "onExitCodes": {"containerName": "scrape", "operator": "In", "values": [1, 2, 3, 4]},
        },
    ]
    pod = spec["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    assert pod["terminationGracePeriodSeconds"] == 120


def test_local_job_runs_the_dev_image_as_a_non_root_host_user(local_job: dict[str, Any]) -> None:
    pod = local_job["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    assert container["image"] == "groceries-scraper:dev"
    assert container["imagePullPolicy"] == "Never"
    assert container["resources"] == {
        "requests": {"cpu": "250m", "memory": "512Mi"},
        "limits": {"memory": "1Gi"},
    }
    assert container["envFrom"] == [{"secretRef": {"name": "scrape-sinks", "optional": True}}]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["runAsUser"] == 1000
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert {"name": "HOME", "value": "/tmp"} in container["env"]


def test_local_job_mounts_the_repos_runs_and_sites(local_job: dict[str, Any]) -> None:
    pod = local_job["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    assert {m["mountPath"]: m.get("readOnly", False) for m in container["volumeMounts"]} == {
        "/app/runs": False,
        "/app/sites": True,
    }
    volumes = {v["name"]: v for v in pod["volumes"]}
    mounted = {m["mountPath"]: volumes[m["name"]] for m in container["volumeMounts"]}
    assert mounted["/app/runs"] == {
        "name": "runs",
        "hostPath": {"path": "/mnt/groceries-scraper/runs", "type": "Directory"},
    }
    assert mounted["/app/sites"] == {
        "name": "sites",
        "hostPath": {"path": "/mnt/groceries-scraper/sites", "type": "Directory"},
    }


def test_local_overlay_renders_through_deploy_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)  # the Job runs `sites/…` relative to the image's /app
    result = CliRunner().invoke(
        app, ["deploy", "job", "sites/aldi.yaml"], input=_build(LOCAL), catch_exceptions=False
    )

    assert result.exit_code == 0, result.output
    job = yaml.safe_load(result.stdout)
    assert job["metadata"]["generateName"] == "scrape-aldi-default-"
    (container,) = job["spec"]["template"]["spec"]["containers"]
    assert container["args"] == ["run", "sites/aldi.yaml", "--location", "default"]
    assert "--archive" not in container["args"]
