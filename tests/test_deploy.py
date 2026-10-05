from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from groceries_scraper.cli import app

TEMPLATE = Path(__file__).parent / "fixtures" / "job.yaml"


def _site(tmp_path: Path, **changes: Any) -> Path:
    config = tmp_path / "site.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "site": "aldi",
                "start": [{"url": "https://shop.example/", "page_type": "listing"}],
                "page_types": {"listing": {}},
                **changes,
            }
        )
    )
    return config


def test_renders_one_run_and_preserves_the_overlays_cluster_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    config = _site(tmp_path, locations={"north": {}})
    template = yaml.safe_load(TEMPLATE.read_text())
    container = template["spec"]["template"]["spec"]["containers"][0]
    container["args"] = ["--archive", "s3://bucket/from-overlay"]

    result = CliRunner().invoke(
        app,
        [
            "deploy",
            "job",
            str(config),
            "--location",
            "north",
            "--sink",
            "postgres://user@db/scrape",
            "--sink",
            "s3://bucket/records",
            "--archive",
            "s3://bucket/archives",
        ],
        input=yaml.safe_dump(template),
    )

    assert result.exit_code == 0, result.output
    job = yaml.safe_load(result.stdout)
    assert job["apiVersion"] == "batch/v1"
    assert job["kind"] == "Job"
    assert job["metadata"] == {
        "generateName": "scrape-aldi-north-",
        "namespace": "groceries",
        "labels": {
            "app": "scraper",
            "groceries-scraper/site": "aldi",
            "groceries-scraper/location": "north",
        },
    }
    pod = job["spec"]["template"]
    assert pod["metadata"]["labels"] == job["metadata"]["labels"]
    rendered = pod["spec"]["containers"][0]
    assert rendered["args"] == [
        "--archive",
        "s3://bucket/from-overlay",
        "run",
        str(config),
        "--location",
        "north",
        "--sink",
        "postgres://user@db/scrape",
        "--sink",
        "s3://bucket/records",
        "--archive",
        "s3://bucket/archives",
    ]
    assert rendered["envFrom"] == [
        {"secretRef": {"name": "scrape-sinks", "optional": True}},
        {"secretRef": {"name": "scrape-site-aldi", "optional": True}},
    ]
    assert rendered["env"] == [
        {"name": "KEEP", "value": "template"},
        {
            "name": "SCRAPE_JOB_NAME",
            "valueFrom": {
                "fieldRef": {"fieldPath": "metadata.labels['batch.kubernetes.io/job-name']"}
            },
        },
    ]
    assert rendered["resources"] == container["resources"]
    assert rendered["volumeMounts"] == container["volumeMounts"]
    assert pod["spec"]["volumes"] == template["spec"]["template"]["spec"]["volumes"]
    assert pod["spec"]["serviceAccountName"] == "scrape"
    assert job["spec"]["backoffLimit"] == 2
    assert job["spec"]["activeDeadlineSeconds"] == 14400


@pytest.mark.parametrize(
    ("site", "location", "prefix"),
    [
        ("ALDI_Picks", "North_Sydney", "scrape-aldi-picks-north-sydney-"),
        ("A" * 30, "N" * 30, "scrape-" + "a" * 30 + "-" + "n" * 19 + "-"),
        ("aldi", "north__Sydney", "scrape-aldi-north--sydney-"),
    ],
)
def test_names_are_sanitised_and_fit_with_the_generated_suffix(
    tmp_path: Path, site: str, location: str, prefix: str
) -> None:
    config = _site(tmp_path, site=site, locations={location: {}})
    result = CliRunner().invoke(
        app, ["deploy", "job", str(config), "--location", location], input=TEMPLATE.read_text()
    )

    assert result.exit_code == 0, result.output
    job = yaml.safe_load(result.stdout)
    assert job["metadata"]["generateName"] == prefix
    assert job["metadata"]["labels"]["groceries-scraper/site"] == site
    assert job["metadata"]["labels"]["groceries-scraper/location"] == location
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["envFrom"][-1] == {
        "secretRef": {"name": "scrape-site-" + site.lower().replace("_", "-"), "optional": True}
    }


def test_browser_page_types_get_shared_memory_and_a_memory_bump(tmp_path: Path) -> None:
    config = _site(tmp_path, page_types={"listing": {}, "detail": {"render": "browser"}})
    result = CliRunner().invoke(app, ["deploy", "job", str(config)], input=TEMPLATE.read_text())

    assert result.exit_code == 0, result.output
    pod_spec = yaml.safe_load(result.stdout)["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    assert container["resources"] == {
        "requests": {"cpu": "250m", "memory": "1Gi"},
        "limits": {"memory": "2Gi"},
    }
    assert container["volumeMounts"] == [
        {"name": "runs", "mountPath": "/app/runs"},
        {"name": "scrape-shm", "mountPath": "/dev/shm"},
    ]
    assert pod_spec["volumes"] == [
        {"name": "runs", "emptyDir": {}},
        {"name": "scrape-shm", "emptyDir": {"medium": "Memory"}},
    ]
    assert "unreachable" in result.stderr


def test_a_site_without_locations_uses_default(tmp_path: Path) -> None:
    config = _site(tmp_path)
    result = CliRunner().invoke(app, ["deploy", "job", str(config)], input=TEMPLATE.read_text())

    assert result.exit_code == 0, result.output
    job = yaml.safe_load(result.stdout)
    assert job["metadata"]["generateName"] == "scrape-aldi-default-"
    assert job["metadata"]["labels"]["groceries-scraper/location"] == "default"
    assert job["spec"]["template"]["spec"]["containers"][0]["args"] == [
        "run",
        str(config),
        "--location",
        "default",
    ]


@pytest.mark.parametrize(
    ("locations", "options", "message"),
    [
        ({"north": {}, "south": {}}, [], "--location is required; the Site declares: north, south"),
        (
            {"north": {}},
            ["--location", "west"],
            "unknown Location `west`; the Site declares: north",
        ),
        ({}, ["--location", "west"], "unknown Location `west`; the Site declares none"),
    ],
)
def test_location_errors_match_run(
    tmp_path: Path, locations: dict[str, Any], options: list[str], message: str
) -> None:
    config = _site(tmp_path, locations=locations)
    runner = CliRunner()
    result = runner.invoke(
        app, ["deploy", "job", str(config), *options], input=TEMPLATE.read_text()
    )
    run = runner.invoke(app, ["run", str(config), *options])

    assert result.exit_code == run.exit_code == 1
    assert result.stderr.strip() == run.stderr.strip() == message
    assert result.stdout == ""


@pytest.mark.parametrize(
    "template",
    [
        "",
        "---\n",
        "[]",
        "not YAML: [",
        "kind: Pod\napiVersion: v1\n",
        TEMPLATE.read_text() + "\n---\n" + TEMPLATE.read_text(),
        TEMPLATE.read_text() + "\n---\n",
        "apiVersion: batch/v1\nkind: Job\n",
        TEMPLATE.read_text().replace("containers:", "containers: []\n      unused:"),
    ],
)
def test_stdin_must_be_exactly_one_job_template(tmp_path: Path, template: str) -> None:
    config = _site(tmp_path)
    result = CliRunner().invoke(app, ["deploy", "job", str(config)], input=template)

    assert result.exit_code == 1
    assert "exactly one batch/v1 Job" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("password", ["secret", "p%40ss", ""])
@pytest.mark.parametrize("source", ["cli", "template", "template-equals"])
def test_sink_passwords_are_refused_without_printing_the_url(
    tmp_path: Path, password: str, source: str
) -> None:
    config = _site(tmp_path)
    sink_url = f"postgres://user:{password}@db/groceries"
    template = yaml.safe_load(TEMPLATE.read_text())
    options = ["--sink", sink_url] if source == "cli" else []
    if source != "cli":
        template["spec"]["template"]["spec"]["containers"][0]["args"] = (
            [f"--sink={sink_url}"] if source == "template-equals" else ["--sink", sink_url]
        )

    result = CliRunner().invoke(
        app, ["deploy", "job", str(config), *options], input=yaml.safe_dump(template)
    )

    assert result.exit_code == 1
    assert "PGPASSWORD" in result.stderr
    assert sink_url not in result.output
    assert result.stdout == ""


def test_browser_settings_replace_existing_run_fields_without_duplicates(tmp_path: Path) -> None:
    config = _site(tmp_path, page_types={"listing": {"render": "browser"}})
    template = yaml.safe_load(TEMPLATE.read_text())
    pod_spec = template["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    container["env"].append({"name": "SCRAPE_JOB_NAME", "value": "placeholder"})
    container["envFrom"].append({"secretRef": {"name": "scrape-site-aldi", "optional": False}})
    container["volumeMounts"].append({"name": "scrape-shm", "mountPath": "/dev/shm"})
    pod_spec["volumes"].append({"name": "scrape-shm", "emptyDir": {}})
    pod_spec["containers"].append({"name": "sidecar", "image": "helper:dev", "args": ["watch"]})

    result = CliRunner().invoke(app, ["deploy", "job", str(config)], input=yaml.safe_dump(template))

    assert result.exit_code == 0, result.output
    rendered = yaml.safe_load(result.stdout)["spec"]["template"]["spec"]
    scraper = rendered["containers"][0]
    assert [e for e in scraper["env"] if e["name"] == "SCRAPE_JOB_NAME"] == [
        {
            "name": "SCRAPE_JOB_NAME",
            "valueFrom": {
                "fieldRef": {"fieldPath": "metadata.labels['batch.kubernetes.io/job-name']"}
            },
        }
    ]
    assert scraper["envFrom"] == [
        {"secretRef": {"name": "scrape-sinks", "optional": True}},
        {"secretRef": {"name": "scrape-site-aldi", "optional": True}},
    ]
    assert [m for m in scraper["volumeMounts"] if m["mountPath"] == "/dev/shm"] == [
        {"name": "scrape-shm", "mountPath": "/dev/shm"}
    ]
    assert [v for v in rendered["volumes"] if v["name"] == "scrape-shm"] == [
        {"name": "scrape-shm", "emptyDir": {"medium": "Memory"}}
    ]
    assert rendered["containers"][1] == pod_spec["containers"][1]


@pytest.mark.parametrize(
    ("site", "location"),
    [("A" * 64, "north"), ("aldi", "N" * 64), ("aldi", "_north_")],
)
def test_names_that_cannot_be_exact_kubernetes_labels_are_rejected(
    tmp_path: Path, site: str, location: str
) -> None:
    config = _site(tmp_path, site=site, locations={location: {}})
    result = CliRunner().invoke(
        app, ["deploy", "job", str(config), "--location", location], input=TEMPLATE.read_text()
    )

    assert result.exit_code == 1
    assert "Kubernetes label" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("field", ["args", "env", "envFrom", "volumeMounts", "resources"])
def test_malformed_container_fields_are_reported_as_bad_templates(
    tmp_path: Path, field: str
) -> None:
    config = _site(tmp_path, page_types={"listing": {"render": "browser"}})
    template = yaml.safe_load(TEMPLATE.read_text())
    template["spec"]["template"]["spec"]["containers"][0][field] = "invalid"
    result = CliRunner().invoke(app, ["deploy", "job", str(config)], input=yaml.safe_dump(template))

    assert result.exit_code == 1
    assert "exactly one batch/v1 Job" in result.stderr
    assert result.stdout == ""


def test_optional_template_fields_can_be_absent_or_null(tmp_path: Path) -> None:
    config = _site(tmp_path, page_types={"listing": {"render": "browser"}})
    template = """
apiVersion: batch/v1
kind: Job
metadata:
spec:
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: scrape
          image: groceries-scraper:dev
          args:
          env:
          envFrom:
          resources:
          volumeMounts:
      volumes:
"""
    result = CliRunner().invoke(app, ["deploy", "job", str(config)], input=template)

    assert result.exit_code == 0, result.output
    job = yaml.safe_load(result.stdout)
    assert job["metadata"]["generateName"] == "scrape-aldi-default-"
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"] == {"requests": {"memory": "1Gi"}, "limits": {"memory": "2Gi"}}
