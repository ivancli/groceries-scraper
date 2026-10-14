"""A stand-in search child speaking the supervisor's IPC; the query picks its behaviour."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from typing import Any

channel = socket.socket(fileno=int(sys.argv[1]))
reader = channel.makefile("r", encoding="utf-8")


def send(message: dict[str, Any]) -> None:
    channel.sendall((json.dumps(message) + "\n").encode())


def receive() -> dict[str, Any]:
    line = reader.readline()
    if not line:
        sys.exit(9)
    message: dict[str, Any] = json.loads(line)
    return message


def permit(paced: bool = True) -> int | None:
    send({"type": "permit", "id": 1, "paced": paced, "charge": False})
    reply = receive()
    granted: int | None = reply["granted"]
    return granted


def result(status: str = "ok", results: list[dict[str, Any]] | None = None) -> None:
    send({"type": "result", "status": status, "results": results or [], "error": None})


job = receive()
mode, _, arg = job["query"].partition(" ")
milk = [{"title": "Milk 2L", "url": "https://shop.example/p/milk"}]

if mode == "ok":
    granted = permit()
    if granted is None:
        result("failed")
    else:
        send({"type": "release", "permit": granted})
        result("ok", milk)
elif mode == "slow":
    time.sleep(float(arg or 1))
    result("ok", milk)
elif mode == "env":
    result("ok", [{"env": sorted(os.environ), "cwd": os.getcwd(), "budget": job["budget"]}])
elif mode == "crash":
    sys.exit(3)
elif mode == "hang":
    time.sleep(3600)
elif mode == "grandchild":
    # Its own descendant, as a browser would be; the marker lets the test find it.
    subprocess.Popen(["sleep", arg.split()[0]])
    if job["query"].endswith("result"):
        result("ok", milk)
    time.sleep(3600)
elif mode == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(3600)
elif mode == "late":
    time.sleep(job["budget"] + 0.5)
    result("ok", milk)
elif mode == "oversize":
    result("ok", [{"title": "x" * 200_000, "url": "https://shop.example/p/x"}])
    time.sleep(3600)
elif mode == "permit-later":
    time.sleep(float(arg))
    granted = permit()
    result("failed" if granted is None else "ok", [] if granted is None else milk)
elif mode == "garbage":
    channel.sendall(b"not json\n")
    time.sleep(3600)
