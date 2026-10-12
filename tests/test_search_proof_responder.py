import http.client
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Thread
from typing import Any

import pytest

from groceries_scraper.search.proof import ProofResponder

TOKEN = "origin-token"


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "token"
    path.write_text(TOKEN + "\n")
    return path


@contextmanager
def serving(token_file: Path, terminal_delay: float) -> Iterator[ProofResponder]:
    server = ProofResponder(("127.0.0.1", 0), token_file=token_file, terminal_delay=terminal_delay)
    Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def responder(token_file: Path) -> Iterator[ProofResponder]:
    with serving(token_file, terminal_delay=0.5) as server:
        yield server


def connect(server: ProofResponder) -> http.client.HTTPConnection:
    return http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)


def search(**overrides: Any) -> dict[str, Any]:
    return {
        "contract_version": 1,
        "request_id": "11111111-1111-4111-8111-111111111111",
        "execution_key": "fixture-key",
        "retailer": "fixture",
        "adapter_version": "fixture-v1",
        "query": "ok",
        "context": {},
        "budget_ms": 10_000,
        "admission_budget_ms": 2_000,
        "expires_at": "2099-01-01T00:00:10Z",
        **overrides,
    }


def post(
    server: ProofResponder, body: bytes | dict[str, Any], token: str | None = TOKEN
) -> http.client.HTTPResponse:
    connection = connect(server)
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    payload = body if isinstance(body, bytes) else json.dumps(body).encode()
    connection.request("POST", "/v1/search", body=payload, headers=headers)
    return connection.getresponse()


def test_acceptance_is_flushed_before_the_delayed_terminal_frame(
    responder: ProofResponder,
) -> None:
    started = time.monotonic()
    response = post(responder, search())

    assert response.status == 200
    assert response.getheader("Content-Type") == "application/x-ndjson"
    accepted = json.loads(response.readline())
    accepted_after = time.monotonic() - started
    terminal = json.loads(response.readline())
    terminal_after = time.monotonic() - started

    assert accepted["type"] == "accepted"
    assert accepted["contract_version"] == 1
    assert accepted["request_id"] == search()["request_id"]
    assert accepted["adapter_version"] == "fixture-v1"
    assert accepted["joined"] is False
    assert terminal == {
        "contract_version": 1,
        "type": "result",
        "request_id": accepted["request_id"],
        "execution_id": accepted["execution_id"],
        "status": "ok",
        "source_at": terminal["source_at"],
        "results": [],
    }
    assert accepted_after < 0.25 < 0.5 <= terminal_after
    assert response.read() == b""  # EOF after exactly two frames


def rejection(response: http.client.HTTPResponse) -> tuple[int, Any]:
    assert response.getheader("Content-Type") == "application/json"
    return response.status, json.loads(response.read())


@pytest.mark.parametrize("token", [None, "wrong-token", "", TOKEN + "x"])
def test_a_missing_or_wrong_origin_credential_is_rejected_before_admission(
    responder: ProofResponder, token: str | None
) -> None:
    assert rejection(post(responder, search(), token=token)) == (
        401,
        {"contract_version": 1, "type": "rejected", "code": "unauthorized"},
    )


def test_a_rotated_credential_takes_effect_without_a_restart(
    responder: ProofResponder, token_file: Path
) -> None:
    token_file.write_text("rotated-token")

    assert rejection(post(responder, search(), token=TOKEN))[0] == 401
    assert post(responder, search(), token="rotated-token").status == 200


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        json.dumps({k: v for k, v in search().items() if k != "request_id"}).encode(),
        json.dumps(search(contract_version=2)).encode(),
        json.dumps(search(retailer="coles")).encode(),  # fixtures only: no real retailer
        json.dumps(search(adapter_version="coles-live-v1")).encode(),
        json.dumps(search(query="unknown-scenario")).encode(),
        json.dumps(search(budget_ms=10_001)).encode(),
        json.dumps(search(admission_budget_ms=2_001)).encode(),
        json.dumps(search(admission_budget_ms=0)).encode(),
        json.dumps(search(budget_ms=True)).encode(),
        json.dumps(search(context=[])).encode(),
        json.dumps(search(expires_at="2000-01-01T00:00:00Z")).encode(),  # already expired
    ],
)
def test_an_invalid_request_is_rejected_before_admission(
    responder: ProofResponder, body: bytes
) -> None:
    assert rejection(post(responder, body)) == (
        400,
        {"contract_version": 1, "type": "rejected", "code": "invalid_request"},
    )


def test_a_body_over_four_kib_is_rejected_unread(responder: ProofResponder) -> None:
    body = json.dumps(search(context={"padding": "x" * 4096})).encode()

    assert rejection(post(responder, body))[1]["code"] == "too_large"


def test_only_the_search_endpoint_is_served(responder: ProofResponder) -> None:
    connection = connect(responder)
    connection.request(
        "POST", "/v1/other", body=b"{}", headers={"Authorization": f"Bearer {TOKEN}"}
    )

    assert rejection(connection.getresponse()) == (
        404,
        {"contract_version": 1, "type": "rejected", "code": "not_found"},
    )


def test_a_second_search_is_busy_until_the_admitted_one_finishes(
    responder: ProofResponder,
) -> None:
    first = post(responder, search())
    first.readline()  # admitted: the single execution slot is held

    assert rejection(post(responder, search(request_id="second"))) == (
        503,
        {"contract_version": 1, "type": "rejected", "code": "busy"},
    )
    first.read()
    assert post(responder, search(request_id="third")).status == 200


def test_a_malformed_content_length_is_an_invalid_request(responder: ProofResponder) -> None:
    connection = connect(responder)
    connection.putrequest("POST", "/v1/search")
    connection.putheader("Authorization", f"Bearer {TOKEN}")
    connection.putheader("Content-Length", "-1")
    connection.endheaders()

    assert rejection(connection.getresponse())[1]["code"] == "invalid_request"


def test_a_disconnected_caller_frees_the_slot_without_waiting_for_its_terminal_frame(
    token_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with serving(token_file, terminal_delay=30) as server:
        abandoned = post(server, search())
        abandoned.readline()
        abandoned.close()  # the Worker gave up, e.g. its deadline passed

        deadline = time.monotonic() + 2
        while (response := post(server, search(request_id="next"))).status == 503:
            assert time.monotonic() < deadline, "the slot was never released"
            time.sleep(0.05)

        assert response.status == 200
    assert "Traceback" not in capsys.readouterr().err


def test_the_terminal_frame_never_waits_past_the_execution_budget(
    responder: ProofResponder,
) -> None:
    started = time.monotonic()
    response = post(responder, search(budget_ms=200))
    accepted = json.loads(response.readline())
    terminal = json.loads(response.readline())

    assert 0.2 <= time.monotonic() - started < 0.45  # the fixture's own delay is 0.5s
    assert terminal == {
        "contract_version": 1,
        "type": "error",
        "request_id": accepted["request_id"],
        "execution_id": accepted["execution_id"],
        "code": "deadline_exceeded",
    }
    assert response.read() == b""


def frames(response: http.client.HTTPResponse) -> list[dict[str, Any]]:
    return [json.loads(line) for line in response.read().splitlines()]


def test_the_fragmented_scenario_splits_frames_across_many_chunks(
    responder: ProofResponder,
) -> None:
    response = post(responder, search(query="fragmented"))
    pieces = []
    while piece := response.read1(64):
        pieces.append(piece)

    assert len(pieces) > 4
    assert [frame["type"] for frame in map(json.loads, b"".join(pieces).splitlines())] == [
        "accepted",
        "result",
    ]


def test_the_oversize_scenario_sends_a_terminal_frame_over_64_kib(
    responder: ProofResponder,
) -> None:
    accepted, terminal = post(responder, search(query="oversize")).read().splitlines()

    assert len(terminal) > 64 * 1024
    assert json.loads(terminal)["execution_id"] == json.loads(accepted)["execution_id"]


def test_the_mismatched_scenario_answers_for_another_execution(
    responder: ProofResponder,
) -> None:
    accepted, terminal = frames(post(responder, search(query="mismatched")))

    assert terminal["type"] == "result"
    assert terminal["request_id"] == accepted["request_id"]
    assert terminal["execution_id"] != accepted["execution_id"]


def test_the_duplicate_scenario_accepts_twice(responder: ProofResponder) -> None:
    types = [frame["type"] for frame in frames(post(responder, search(query="duplicate")))]

    assert types == ["accepted", "accepted", "result"]


def test_the_premature_scenario_ends_the_stream_after_acceptance(
    responder: ProofResponder,
) -> None:
    assert [frame["type"] for frame in frames(post(responder, search(query="premature")))] == [
        "accepted"
    ]


def test_the_late_scenario_accepts_only_after_the_admission_budget(
    responder: ProofResponder,
) -> None:
    started = time.monotonic()
    response = post(responder, search(query="late", admission_budget_ms=100))
    response.readline()

    assert time.monotonic() - started >= 0.1


def test_the_health_probe_needs_no_credential(responder: ProofResponder) -> None:
    connection = connect(responder)
    connection.request("GET", "/healthz")
    response = connection.getresponse()

    assert (response.status, response.read()) == (200, b"ok\n")
