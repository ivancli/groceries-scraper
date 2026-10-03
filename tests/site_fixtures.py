"""Replay Golden Fixtures with network connections refused and report Record diffs."""

import difflib
import json
import os
import subprocess
import sys
from pathlib import Path

from groceries_scraper.run.fixtures import record_data

OFFLINE_REPLAY = """
import os, socket, sys
def refuse_network(event, args):
    if event in ('socket.connect', 'socket.sendto', 'socket.sendmsg'):
        if args[0].family not in (socket.AF_INET, socket.AF_INET6):
            return
    elif event not in (
        'socket.getaddrinfo', 'socket.gethostbyname', 'socket.gethostbyaddr', 'socket.getnameinfo'
    ):
        return
    with open(os.environ['NETWORK_LOG'], 'a') as log:
        log.write(event + '\\n')
    raise OSError('Golden Fixtures cannot access the network')
sys.addaudithook(refuse_network)
from groceries_scraper.cli import app
app()
"""


def assert_site_fixture(fixture: Path, config: Path, output: Path) -> None:
    network_log = output / "network.log"
    result = subprocess.run(
        [sys.executable, "-c", OFFLINE_REPLAY, "replay", str(fixture), "--config", str(config)],
        cwd=output,
        env={**os.environ, "NETWORK_LOG": str(network_log)},
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert not network_log.exists(), "Golden Fixture attempted network access"
    runs = list((output / "runs" / fixture.name).glob("*"))
    assert len(runs) == 1, result.stdout + result.stderr
    replay = runs[0]
    expected = record_data(fixture / "records")
    actual = record_data(replay / "records")
    if expected != actual:
        diff = "".join(
            difflib.unified_diff(
                (
                    json.dumps(expected, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
                ).splitlines(keepends=True),
                (
                    json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
                ).splitlines(keepends=True),
                fromfile="expected Records",
                tofile="replayed Records",
            )
        )
        raise AssertionError(diff)
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads((replay / "run.json").read_text(encoding="utf-8"))
    assert manifest["stats"]["requests"]["missing"] == 0, "Replay has missing Captures"
