import base64
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


@pytest.fixture(scope="module")
def local_postgres() -> dict[str, dict[str, Any]]:
    return {doc["kind"]: doc for doc in yaml.safe_load_all(_build(LOCAL / "postgres"))}


def test_local_postgres_keeps_its_data_on_a_local_path_volume(
    local_postgres: dict[str, dict[str, Any]],
) -> None:
    postgres = local_postgres["StatefulSet"]
    (claim,) = postgres["spec"]["volumeClaimTemplates"]
    assert claim["spec"]["storageClassName"] == "local-path"
    (container,) = postgres["spec"]["template"]["spec"]["containers"]
    assert container["image"] == "postgres:16-alpine"
    assert {"name": claim["metadata"]["name"], "mountPath": "/var/lib/postgresql/data"} in (
        container["volumeMounts"]
    )
    service = local_postgres["Service"]
    assert service["metadata"]["name"] == postgres["spec"]["serviceName"] == "postgres"


def test_scrape_sinks_points_every_job_at_local_postgres(
    local_postgres: dict[str, dict[str, Any]],
) -> None:
    secret = local_postgres["Secret"]
    assert secret["metadata"]["name"] == "scrape-sinks"  # unhashed: Jobs reference it by name
    assert set(secret["data"]) == {"SCRAPE_SINK", "PGPASSWORD"}
    sink = base64.b64decode(secret["data"]["SCRAPE_SINK"]).decode()
    assert sink == "postgresql://scrape@postgres/scrape"
    (container,) = local_postgres["StatefulSet"]["spec"]["template"]["spec"]["containers"]
    password = {"secretKeyRef": {"name": "scrape-sinks", "key": "PGPASSWORD"}}
    assert {"name": "POSTGRES_PASSWORD", "valueFrom": password} in container["env"]


def test_backups_run_nightly_to_the_host_as_its_user(
    local_postgres: dict[str, dict[str, Any]],
) -> None:
    backup = local_postgres["CronJob"]
    assert backup["spec"]["schedule"] == "0 3 * * *"
    assert backup["spec"]["concurrencyPolicy"] == "Forbid"
    pod = backup["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsUser"] == pod["securityContext"]["runAsGroup"] == 1000
    (volume,) = pod["volumes"]
    assert volume["hostPath"] == {"path": "/mnt/groceries-scraper/backups", "type": "Directory"}


@pytest.fixture(scope="module")
def local_dispatcher() -> dict[str, dict[str, Any]]:
    return {doc["kind"]: doc for doc in yaml.safe_load_all(_build(LOCAL / "dispatcher"))}


def test_the_dispatcher_ticks_every_five_minutes_one_at_a_time(
    local_dispatcher: dict[str, dict[str, Any]],
) -> None:
    cron = local_dispatcher["CronJob"]
    assert cron["spec"]["schedule"] == "*/5 * * * *"
    assert cron["spec"]["concurrencyPolicy"] == "Forbid"
    pod = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == local_dispatcher["ServiceAccount"]["metadata"]["name"]
    (container,) = pod["containers"]
    assert container["image"] == "groceries-scraper:dev"
    assert container["args"] == ["dispatch"]
    assert container["envFrom"] == [
        {"secretRef": {"name": "scrape-sinks"}},
        {"secretRef": {"name": "scrape-dispatcher"}},
    ]
    mounts = {mount["name"]: mount["mountPath"] for mount in container["volumeMounts"]}
    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    assert volumes["sites"]["hostPath"]["path"] == "/mnt/groceries-scraper/sites"
    assert mounts["sites"] == "/app/sites"
    assert volumes["job-template"]["configMap"]["name"] == "scrape-job-template"
    env = {item["name"]: item["value"] for item in container["env"]}
    assert env["SCRAPE_JOB_TEMPLATE"].startswith(mounts["job-template"] + "/")


def test_the_dispatcher_may_only_create_and_get_jobs_and_config_maps(
    local_dispatcher: dict[str, dict[str, Any]],
) -> None:
    role = local_dispatcher["Role"]
    assert sorted(
        (rule["apiGroups"], rule["resources"], sorted(rule["verbs"])) for rule in role["rules"]
    ) == [([""], ["configmaps"], ["create", "get"]), (["batch"], ["jobs"], ["create", "get"])]
    binding = local_dispatcher["RoleBinding"]
    assert binding["roleRef"]["name"] == role["metadata"]["name"]
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": local_dispatcher["ServiceAccount"]["metadata"]["name"]}
    ]


def test_the_job_template_the_dispatcher_reads_is_the_local_job(
    local_job: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(ROOT)
    result = CliRunner().invoke(
        app,
        [
            "deploy",
            "job",
            "sites/aldi_picks.yaml",
            "--supply",
            "cm",
            "--run-id",
            "20261006T120000Z-abc123",
            "--record",
            "errors",
        ],
        input=yaml.safe_dump(local_job),
    )

    assert result.exit_code == 0, result.output
    pod = yaml.safe_load(result.stdout)["spec"]["template"]["spec"]
    assert {"name": "supply", "configMap": {"name": "cm"}} in pod["volumes"]


@pytest.fixture(scope="module")
def local_janitor() -> dict[str, Any]:
    (cron,) = yaml.safe_load_all(_build(LOCAL / "janitor"))
    assert isinstance(cron, dict)
    return cron


def test_the_janitor_sweeps_the_hosts_runs_daily_without_cluster_access(
    local_janitor: dict[str, Any], local_job: dict[str, Any]
) -> None:
    assert local_janitor["kind"] == "CronJob"
    assert local_janitor["spec"]["schedule"] == "30 3 * * *"
    assert local_janitor["spec"]["concurrencyPolicy"] == "Forbid"
    assert local_janitor["spec"]["startingDeadlineSeconds"] == 86400
    pod = local_janitor["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsUser"] == pod["securityContext"]["runAsGroup"] == 1000
    (container,) = pod["containers"]
    assert container["image"] == "groceries-scraper:dev"
    assert container["args"] == ["janitor"]
    assert container["envFrom"] == [{"secretRef": {"name": "scrape-sinks"}}]
    # The same host directory, at the same path, that Jobs write their Runs to.
    job_pod = local_job["spec"]["template"]["spec"]
    job_runs = {v["name"]: v for v in job_pod["volumes"]}["runs"]["hostPath"]
    (volume,) = pod["volumes"]
    assert volume["hostPath"] == job_runs
    (mount,) = container["volumeMounts"]
    assert mount == {"name": volume["name"], "mountPath": "/app/runs"}
