import json

import yaml

from groceries_scraper.config.models import SetupStep
from groceries_scraper.engine.pipe import PipeContext
from groceries_scraper.engine.session import Refresh, SessionRefresh, evaluate_setup, setup_request

HOME = b'<html><head><meta name="csrf-token" content="t0k"></head></html>'


def _step(source: str) -> SetupStep:
    return SetupStep.model_validate(yaml.safe_load(source))


CSRF_STEP = """
request: {url: "https://shop.example/"}
extract:
  csrf: {css: "meta[name=csrf-token]::attr(content)"}
"""

JSON_STEP = """
request: {url: "https://shop.example/api/session"}
extract:
  token: {jsonpath: "$.token"}
"""


def test_setup_extracts_session_variables_from_html() -> None:
    result = evaluate_setup(_step(CSRF_STEP), 200, HOME, "text/html", PipeContext())

    assert result.error is None
    assert result.session == {"csrf": "t0k"}


def test_setup_reads_a_json_response_by_its_content_type() -> None:
    step = _step(JSON_STEP)
    body = json.dumps({"token": 42}).encode()

    result = evaluate_setup(step, 200, body, "application/json; charset=utf-8", PipeContext())

    assert result.session == {"token": 42}


def test_setup_fails_on_a_non_2xx_response() -> None:
    result = evaluate_setup(_step(CSRF_STEP), 503, HOME, "text/html", PipeContext())

    assert result.error == "Session Setup request got HTTP 503"
    assert result.session == {}


def test_setup_fails_when_a_session_variable_has_no_value() -> None:
    body = b"<html><head></head></html>"

    result = evaluate_setup(_step(CSRF_STEP), 200, body, "text/html", PipeContext())

    assert result.error == "Session Variable `csrf` has no value"
    assert result.session == {}
    [trace] = result.trace
    assert (trace.name, trace.steps[0].output) == ("csrf", [])


def test_setup_fails_on_an_unparseable_json_response() -> None:
    step = _step(JSON_STEP)

    result = evaluate_setup(step, 200, b"<html>", "application/json", PipeContext())

    assert result.error is not None
    assert result.error.startswith("Session Setup response is not JSON")


def test_setup_request_renders_earlier_session_variables_and_env() -> None:
    step = _step("""
    request:
      method: POST
      url: "https://shop.example/api/session"
      headers: {X-CSRF-Token: "{{ session.csrf }}"}
      json: {key: "{{ env.KEY }}", n: "{{ session.n }}"}
    """)
    ctx = PipeContext(session={"csrf": "t0k", "n": 2}, env={"KEY": "k"})

    request = setup_request(step, ctx)

    assert request.method == "POST"
    assert request.url == "https://shop.example/api/session"
    assert request.headers == {"X-CSRF-Token": "t0k", "Content-Type": "application/json"}
    assert json.loads(request.body or "") == {"key": "k", "n": 2}


def test_a_status_outside_refresh_on_proceeds() -> None:
    refresh = SessionRefresh(refresh_on=[419], max_refresh=1)

    assert refresh.on_status(200, refresh.generation) is Refresh.PROCEED
    assert refresh.on_status(404, refresh.generation) is Refresh.PROCEED


def test_a_refresh_status_refreshes_then_retries_requests_sent_with_the_old_session() -> None:
    refresh = SessionRefresh(refresh_on=[403, 419], max_refresh=1)
    sent_with = refresh.generation

    assert refresh.on_status(419, sent_with) is Refresh.REFRESH
    assert refresh.on_status(403, sent_with) is Refresh.WAIT  # Setup is still running
    refresh.refreshed()
    assert refresh.on_status(419, sent_with) is Refresh.RETRY  # already refreshed
    assert refresh.refreshes == 1


def test_refreshing_stops_at_max_refresh() -> None:
    refresh = SessionRefresh(refresh_on=[419], max_refresh=1)
    assert refresh.on_status(419, refresh.generation) is Refresh.REFRESH
    refresh.refreshed()

    assert refresh.on_status(419, refresh.generation) is Refresh.GIVE_UP
    assert refresh.on_status(419, refresh.generation) is Refresh.GIVE_UP
    assert refresh.refreshes == 1


def test_max_refresh_zero_never_refreshes() -> None:
    refresh = SessionRefresh(refresh_on=[419], max_refresh=0)

    assert refresh.on_status(419, refresh.generation) is Refresh.GIVE_UP
