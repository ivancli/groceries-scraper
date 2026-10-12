"""The isolated test Worker driven against the real proof responder, over loopback instead of VPC.

This proves both ends of the framing agree; it is not evidence of the private route itself.
"""

import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parent.parent
WORKER = ROOT / "deploy" / "search-proof" / "worker"
SCRAPE = Path(sys.executable).parent / "scrape"
TOKEN = "origin-token"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


@contextmanager
def responder(token_file: Path, terminal_delay_ms: int) -> Iterator[str]:
    process = subprocess.Popen(
        [
            str(SCRAPE),
            *("proof-responder", "--token-file", str(token_file), "--host", "127.0.0.1"),
            *("--port", "0", "--terminal-delay-ms", str(terminal_delay_ms)),
        ],
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stderr is not None
        match = re.search(r"listening on 127\.0\.0\.1:(\d+)", process.stderr.readline())
        assert match, "the responder did not start"
        yield f"http://127.0.0.1:{match[1]}"
    finally:
        process.kill()
        process.wait()


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "token"
    path.write_text(TOKEN)
    return path


def prove(origin: str, *scenarios: str, token: str = TOKEN) -> dict[str, Any]:
    result = subprocess.run(
        ["node", WORKER / "scripts" / "local-proof.mjs", "--origin", origin, *scenarios],
        env={**os.environ, "SCRAPER_ORIGIN_TOKEN": token},
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    report = json.loads(result.stdout)
    assert isinstance(report, dict)
    return report


def test_every_fixture_scenario_ends_as_the_proof_expects(token_file: Path) -> None:
    with responder(token_file, terminal_delay_ms=300) as origin:
        report = prove(origin)

    outcomes = {run["scenario"]: (run["ok"], run.get("failure")) for run in report["runs"]}
    assert outcomes == {
        "ok": (True, None),
        "fragmented": (True, None),
        "oversize": (False, "oversize_frame"),
        "mismatched": (False, "mismatched_frame"),
        "duplicate": (False, "duplicate_acceptance"),
        "premature": (False, "premature_eof"),
        "late": (False, "admission_timeout"),
    }
    assert report["passed"] is True


def test_acceptance_is_visible_long_before_a_deliberately_delayed_result(
    token_file: Path,
) -> None:
    with responder(token_file, terminal_delay_ms=2500) as origin:
        (run,) = prove(origin, "ok")["runs"]

    assert run["ok"] is True
    assert run["acceptedMs"] < 2000 <= 2500 <= run["terminalMs"] < 10_000


def test_a_wrong_origin_credential_fails_the_proof(token_file: Path) -> None:
    with responder(token_file, terminal_delay_ms=0) as origin:
        report = prove(origin, "ok", token="wrong-token")

    (run,) = report["runs"]
    assert (run["failure"], run["status"], run["code"]) == ("rejected", 401, "unauthorized")
    assert report["passed"] is False


def test_a_stopped_responder_is_unreachable(token_file: Path) -> None:
    with responder(token_file, terminal_delay_ms=0) as origin:
        pass  # stopped on exit: nothing listens on its port any more

    (run,) = prove(origin, "ok")["runs"]
    assert run["failure"] == "unreachable"


def test_a_search_in_flight_makes_the_proof_busy(token_file: Path) -> None:
    with responder(token_file, terminal_delay_ms=5000) as origin:
        held = http.client.HTTPConnection(origin.removeprefix("http://"), timeout=5)
        held.request(
            "POST",
            "/v1/search",
            body=json.dumps(_search()),
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        )
        admitted = held.getresponse()  # kept: dropping it would hang up and free the slot
        admitted.readline()

        (run,) = prove(origin, "ok")["runs"]
        admitted.close()

    assert (run["failure"], run["status"], run["code"]) == ("rejected", 503, "busy")


def test_a_responder_killed_mid_search_is_a_bounded_failure(token_file: Path) -> None:
    with responder(token_file, terminal_delay_ms=5000) as origin:
        proof = subprocess.Popen(
            ["node", WORKER / "scripts" / "local-proof.mjs", "--origin", origin, "ok"],
            env={**os.environ, "SCRAPER_ORIGIN_TOKEN": TOKEN},
            stdout=subprocess.PIPE,
            text=True,
        )
        time.sleep(1)  # admitted, and waiting for the delayed result
    started = time.monotonic()
    stdout, _ = proof.communicate(timeout=15)

    (run,) = json.loads(stdout)["runs"]
    assert run["acceptedMs"] < 2000
    assert run["failure"] == "disconnected"
    assert time.monotonic() - started < 2, "the Worker noticed at once, not at its deadline"


def _search() -> dict[str, Any]:
    return {
        "contract_version": 1,
        "request_id": "held",
        "execution_key": "held",
        "retailer": "fixture",
        "adapter_version": "fixture-v1",
        "query": "ok",
        "context": {},
        "budget_ms": 10_000,
        "admission_budget_ms": 2_000,
        "expires_at": "2099-01-01T00:00:00Z",
    }
