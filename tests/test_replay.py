import base64
import json
from pathlib import Path
from typing import Any

import pytest
from scrapy import Request
from scrapy.http import HtmlResponse

from groceries_scraper.adapter.fingerprint import request_fingerprint
from groceries_scraper.adapter.middlewares import SESSION_NO
from groceries_scraper.adapter.replay import ReplayError, ReplayIndex, SourceRun
from groceries_scraper.run import Run

URL = "https://shop.example/c/dairy"


def _meta(
    request: Request,
    ignore: frozenset[str] = frozenset(),
    status: int = 200,
    headers: dict[str, list[str] | str] | None = None,
    fingerprint: bool = True,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "request": {
            "method": request.method,
            "url": request.url,
            "headers": {"Content-Type": [(request.headers.get("Content-Type") or b"").decode()]},
            "body": base64.b64encode(request.body).decode("ascii"),
            "body_encoding": "base64",
        },
        "response": {"url": request.url, "status": status, "headers": headers or {}},
    }
    if fingerprint:
        meta["request"]["fingerprint"] = request_fingerprint(request, ignore)
    return meta


def test_requests_match_captures_by_fingerprint_under_the_source_ignore_policy() -> None:
    ignore = frozenset({"csrf"})
    captured = Request(f"{URL}?csrf=old&page=2")
    index = ReplayIndex([(_meta(captured, ignore), b"page 2")], ignore)

    response = index.serve(Request(f"{URL}?page=2&csrf=new"))

    assert response is not None
    assert response.body == b"page 2"
    assert index.serve(Request(f"{URL}?page=3&csrf=new")) is None


def test_json_body_keys_in_the_ignore_policy_do_not_affect_matching() -> None:
    ignore = frozenset({"csrf"})
    api = "https://shop.example/api/product"
    captured = Request(
        api,
        method="POST",
        headers={"Content-Type": "application/json"},
        body=b'{"sku": "a", "csrf": "old"}',
    )
    index = ReplayIndex([(_meta(captured, ignore), b"{}")], ignore)

    rotated = Request(
        api,
        method="POST",
        headers={"Content-Type": "application/json"},
        body=b'{"csrf": "new", "sku": "a"}',
    )
    other = rotated.replace(body=b'{"csrf": "new", "sku": "b"}')
    assert index.serve(rotated) is not None
    assert index.serve(other) is None
    assert ReplayIndex([(_meta(captured), b"{}")], frozenset()).serve(rotated) is None


def test_repeated_requests_get_captures_in_order_then_the_last_again() -> None:
    request = Request(URL)
    index = ReplayIndex(
        [(_meta(request, status=503), b"busy"), (_meta(request), b"ok")], frozenset()
    )

    served = [index.serve(request) for _ in range(3)]

    assert [(r.status, r.body) for r in served if r is not None] == [
        (503, b"busy"),
        (200, b"ok"),
        (200, b"ok"),
    ]


def test_served_responses_keep_status_and_headers_but_not_redacted_values() -> None:
    request = Request(URL)
    headers: dict[str, list[str] | str] = {
        "Content-Type": ["text/html; charset=utf-8"],
        "Set-Cookie": "[REDACTED]",
        "X-Repeat": ["a", "b"],
    }
    index = ReplayIndex([(_meta(request, status=404, headers=headers), b"<p/>")], frozenset())

    response = index.serve(request)

    assert isinstance(response, HtmlResponse)
    assert response.url == URL
    assert response.status == 404
    assert response.headers.getlist("X-Repeat") == [b"a", b"b"]
    assert "Set-Cookie" not in response.headers


def test_legacy_captures_without_a_fingerprint_are_indexed_from_their_url() -> None:
    request = Request(f"{URL}?csrf=old", method="POST", body=b"x")
    index = ReplayIndex([(_meta(request, fingerprint=False), b"ok")], frozenset({"csrf"}))

    assert index.serve(request.replace(url=f"{URL}?csrf=new")) is not None


def test_legacy_captures_with_a_redacted_url_are_rejected() -> None:
    meta = _meta(Request(f"{URL}?token=%5BREDACTED%5D"), fingerprint=False)

    with pytest.raises(ReplayError, match="redacted URL"):
        ReplayIndex([(meta, b"")], frozenset())


def _source_run(tmp_path: Path, config: dict[str, Any]) -> Path:
    run_dir = tmp_path / "runs" / "shop" / "20260101T000000Z-abcdef"
    (run_dir / "captures").mkdir(parents=True)
    manifest = {"site": "shop", "run_id": run_dir.name, "config": config}
    (run_dir / "run.json").write_text(json.dumps(manifest))
    return run_dir


def test_a_source_run_loads_its_config_and_captures(tmp_path: Path) -> None:
    config = {
        "site": "shop",
        "settings": {"record_level": "all"},
        "replay": {"ignore_params": ["csrf"]},
    }
    run_dir = _source_run(tmp_path, config)
    meta = {"capture_no": 1, **_meta(Request(f"{URL}?csrf=old"), frozenset({"csrf"}))}
    (run_dir / "captures" / "0001-listing.meta.json").write_text(json.dumps(meta))
    (run_dir / "captures" / "0001-listing.body").write_bytes(b"listing")

    source = SourceRun.load(run_dir)

    assert source.run == Run("shop", run_dir.name, run_dir)
    assert source.config == config
    response = source.index.serve(Request(f"{URL}?csrf=new"))
    assert response is not None and response.body == b"listing"


@pytest.mark.parametrize("level", ["errors", "off"])
def test_a_source_run_without_every_capture_is_rejected(tmp_path: Path, level: str) -> None:
    run_dir = _source_run(tmp_path, {"site": "shop", "settings": {"record_level": level}})

    with pytest.raises(ReplayError, match=f"record_level: {level}"):
        SourceRun.load(run_dir)


def test_a_directory_without_run_json_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ReplayError, match="not a Run directory"):
        SourceRun.load(tmp_path)


def test_a_source_run_serves_captures_in_number_order_past_four_digits(tmp_path: Path) -> None:
    run_dir = _source_run(tmp_path, {"site": "shop", "settings": {"record_level": "all"}})
    request = Request(URL)
    for capture_no, status in ((10000, 200), (9999, 503)):
        stem = run_dir / "captures" / f"{capture_no:04d}-listing"
        meta = {"capture_no": capture_no, **_meta(request, status=status)}
        stem.with_name(f"{stem.name}.meta.json").write_text(json.dumps(meta))
        stem.with_name(f"{stem.name}.body").write_bytes(b"")

    index = SourceRun.load(run_dir).index

    served = [index.serve(request), index.serve(request)]
    assert [response.status for response in served if response is not None] == [503, 200]


@pytest.mark.parametrize("damage", ["truncated run.json", "missing body", "no config"])
def test_a_damaged_source_run_is_rejected(tmp_path: Path, damage: str) -> None:
    run_dir = _source_run(tmp_path, {"site": "shop", "settings": {"record_level": "all"}})
    meta = {"capture_no": 1, **_meta(Request(URL))}
    (run_dir / "captures" / "0001-listing.meta.json").write_text(json.dumps(meta))
    if damage == "truncated run.json":
        (run_dir / "run.json").write_text('{"site": ')
    elif damage == "no config":
        (run_dir / "run.json").write_text('{"site": "shop"}')
    else:
        pass  # the body file was never written

    with pytest.raises(ReplayError):
        SourceRun.load(run_dir)


def test_session_setup_captures_are_served_to_their_own_session() -> None:
    home = Request("https://shop.example/")
    index = ReplayIndex(
        [
            ({**_meta(home), "session_no": 2}, b"two"),
            ({**_meta(home), "session_no": 1}, b"one"),
        ],
        frozenset(),
    )

    first = index.serve(Request(home.url, meta={SESSION_NO: 1}))
    second = index.serve(Request(home.url, meta={SESSION_NO: 2}))

    assert (first and first.body, second and second.body) == (b"one", b"two")
