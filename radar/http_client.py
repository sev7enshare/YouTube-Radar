from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class HttpError(Exception):
    def __init__(self, status: int, message: str, retry_after: int | None = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.retry_after = retry_after


def request_json(url: str, *, method: str = "GET", token: str = "", payload: dict[str, Any] | None = None,
                 params: dict[str, str] | None = None, timeout: int = 45,
                 extra_headers: dict[str, str] | None = None) -> dict[str, Any]:
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    headers = {"Accept": "application/json", "User-Agent": "YouTubeRadar/0.1"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read(2048).decode("utf-8", errors="replace")
        retry = exc.headers.get("Retry-After")
        raise HttpError(exc.code, detail, int(retry) if retry and retry.isdigit() else None) from exc
    if not isinstance(result, dict):
        raise ValueError("Expected JSON object from " + url.split("?")[0])
    return result

