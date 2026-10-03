"""Request identity for Replay, computed before Capture metadata is redacted."""

import codecs
import json
from email.message import Message
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from scrapy import Request
from scrapy.utils.request import fingerprint


def request_fingerprint(request: Request, ignore_params: frozenset[str]) -> str:
    if ignore_params:
        parts = urlsplit(request.url)
        query = urlencode(
            [
                (name, value)
                for name, value in parse_qsl(parts.query, keep_blank_values=True)
                if name not in ignore_params
            ]
        )
        request = request.replace(
            url=urlunsplit(parts._replace(query=query)), body=_body(request, ignore_params)
        )
    return f"scrapy-sha1-v1:{fingerprint(request).hex()}"


def _body(request: Request, ignored: frozenset[str]) -> bytes:
    content_type = Message()
    content_type["Content-Type"] = (request.headers.get("Content-Type", b"") or b"").decode(
        "latin-1"
    )
    mime_type = content_type.get_content_type()
    body = request.body
    if mime_type == "application/json" or mime_type.endswith("+json"):
        try:
            value = json.loads(body)
        except ValueError:
            return body
        if isinstance(value, dict):
            filtered = {key: item for key, item in value.items() if key not in ignored}
            return json.dumps(
                filtered, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
    elif mime_type == "application/x-www-form-urlencoded":
        try:
            charset = content_type.get_content_charset() or "utf-8"
            codecs.lookup(charset)
            pairs = parse_qsl(
                body.decode("ascii"),
                keep_blank_values=True,
                encoding=charset,
                errors="strict",
            )
        except (LookupError, UnicodeError):
            return body
        return urlencode(
            sorted((name, value) for name, value in pairs if name not in ignored)
        ).encode()
    return body
