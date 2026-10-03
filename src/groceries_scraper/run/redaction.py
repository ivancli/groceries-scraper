"""One HTTP exchange's sensitive values, applied to Capture metadata."""

import base64
import binascii
import re
from http.cookies import CookieError, SimpleCookie
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

REDACTED = "[REDACTED]"


class MetadataRedactor:
    def __init__(
        self, metadata: dict[str, Any], sensitive_paths: set[str], sensitive_headers: frozenset[str]
    ) -> None:
        self.metadata = metadata
        self._paths = sensitive_paths
        self._headers = sensitive_headers
        self._secrets: set[str] = set()
        for side in _sides(metadata):
            self._url_credentials(metadata[side]["url"])
            for name, values in metadata[side]["headers"].items():
                if name.lower() in sensitive_headers:
                    for value in values:
                        self._header_secrets(name.lower(), value)
                elif name.lower() in ("location", "content-location", "referer"):
                    for value in values:
                        self._url_credentials(value)
        self._collect(metadata["variables"], "")
        self._pattern = (
            re.compile(
                "|".join(
                    re.escape(value)
                    for value in sorted(self._secrets, key=lambda value: (-len(value), value))
                )
            )
            if self._secrets
            else None
        )
        self._byte_pattern = (
            re.compile(self._pattern.pattern.encode("utf-8")) if self._pattern is not None else None
        )

    def _header_secrets(self, name: str, value: str) -> None:
        if not value:
            return
        self._secrets.add(value)
        if name in ("cookie", "set-cookie"):
            cookie = SimpleCookie()
            try:
                cookie.load(value)
            except CookieError:
                return
            self._secrets.update(morsel.value for morsel in cookie.values() if morsel.value)
        elif name == "authorization":
            scheme, _, credentials = value.partition(" ")
            if credentials:
                self._secrets.add(credentials)
            if scheme.lower() == "basic":
                try:
                    decoded = base64.b64decode(credentials, validate=True).decode("utf-8")
                except (binascii.Error, UnicodeError):
                    return
                self._secrets.update(part for part in decoded.split(":", 1) if part)

    def _collect(self, value: Any, path: str, sensitive: bool = False) -> None:
        sensitive = sensitive or path in self._paths
        if isinstance(value, dict):
            for key, item in value.items():
                self._collect(item, f"{path}.{key}" if path else key, sensitive)
        elif isinstance(value, list):
            for item in value:
                self._collect(item, path, sensitive)
        elif sensitive and str(value):
            self._secrets.add(str(value))

    def _url_credentials(self, url: str) -> None:
        try:
            parts = urlsplit(url)
        except ValueError:
            return
        self._secrets.update(unquote(value) for value in (parts.username, parts.password) if value)

    def _value(self, value: Any, path: str) -> Any:
        if (
            path in self._paths
            or (
                isinstance(value, str) and self._pattern is not None and self._pattern.search(value)
            )
            or (not isinstance(value, dict | list | str) and str(value) in self._secrets)
        ):
            return REDACTED
        if isinstance(value, dict):
            return {
                key: self._value(item, f"{path}.{key}" if path else key)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._value(item, path) for item in value]
        return value

    def _text(self, value: str) -> str:
        return self._pattern.sub(REDACTED, value) if self._pattern is not None else value

    def _encoded_text(self, value: str, query_span: tuple[int, int] | None = None) -> str:
        if self._byte_pattern is None:
            return value
        decoded = bytearray()
        form_decoded = bytearray()
        offsets = []
        # Map decoded bytes back to their spans so public URL encoding stays intact.
        for token in re.finditer(r"%[0-9a-fA-F]{2}|.", value, re.DOTALL):
            raw = token.group()
            chunk = bytes.fromhex(raw[1:]) if len(raw) == 3 and raw[0] == "%" else raw.encode()
            decoded.extend(chunk)
            form_space = (
                raw == "+"
                and query_span is not None
                and query_span[0] <= token.start() < query_span[1]
            )
            form_decoded.extend(b" " if form_space else chunk)
            offsets.extend([(token.start(), token.end())] * len(chunk))
        spans = sorted(
            {
                (offsets[match.start()][0], offsets[match.end() - 1][1])
                for variant in ((decoded, form_decoded) if query_span is not None else (decoded,))
                for match in self._byte_pattern.finditer(variant)
            }
        )
        merged: list[tuple[int, int]] = []
        for start, end in spans:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        pieces: list[str] = []
        cursor = 0
        for start, end in merged:
            pieces.extend((value[cursor:start], quote(REDACTED, safe="")))
            cursor = end
        return "".join([*pieces, value[cursor:]])

    def _url(self, url: str) -> str:
        try:
            parts = urlsplit(url)
        except ValueError:
            return self._text(unquote(url))
        netloc = parts.netloc
        if "@" in netloc:
            netloc = f"{quote(REDACTED, safe='')}@{netloc.rsplit('@', 1)[1]}"
        before_fragment, fragment_marker, _ = url.partition("#")
        query_marker = "?" if "?" in before_fragment else ""
        tail = parts.path + query_marker + parts.query + fragment_marker + parts.fragment
        query_start = len(parts.path) + 1
        query_span = (query_start, query_start + len(parts.query)) if query_marker else None
        # Match across raw delimiters, which can themselves be part of a known secret.
        return urlunsplit((parts.scheme, netloc, "", "", "")) + self._encoded_text(tail, query_span)

    def redact(self) -> dict[str, Any]:
        metadata = {**self.metadata, "variables": self._value(self.metadata["variables"], "")}
        for side in _sides(self.metadata):
            exchange = self.metadata[side]
            headers = {
                name: REDACTED
                if name.lower() in self._headers
                else [
                    self._url(value)
                    if name.lower() in ("location", "content-location", "referer")
                    else self._text(value)
                    for value in values
                ]
                for name, values in exchange["headers"].items()
            }
            metadata[side] = {**exchange, "url": self._url(exchange["url"]), "headers": headers}
        return metadata


def _sides(metadata: dict[str, Any]) -> list[str]:
    """Missing Replay requests have no response."""
    return [side for side in ("request", "response") if side in metadata]
