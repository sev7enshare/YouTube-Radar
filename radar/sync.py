from __future__ import annotations

import base64
import hmac
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlparse

from .config import Config
from .db import artifacts_after, connect
from .http_client import request_json


def make_handler(config: Config) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._json({"ok": True})
                return
            supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
            if not config.sync_token or not hmac.compare_digest(supplied, config.sync_token):
                self._json({"error": "unauthorized"}, 401)
                return
            if parsed.path != "/api/artifacts":
                self._json({"error": "not found"}, 404)
                return
            try:
                after = max(0, int(parse_qs(parsed.query).get("after", ["0"])[0]))
            except ValueError:
                self._json({"error": "invalid cursor"}, 400)
                return
            db = connect(config.db_path)
            try:
                rows = artifacts_after(db, after, 25)
                result = [{"revision": row["revision"], "path": row["path"],
                           "mime": row["mime"], "body_base64": base64.b64encode(row["body"]).decode()}
                          for row in rows]
            finally:
                db.close()
            self._json({"artifacts": result, "has_more": len(result) == 25})

        def _json(self, value: dict, status: int = 200) -> None:
            body = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def start_server(config: Config) -> ThreadingHTTPServer:
    if len(config.sync_token) < 24:
        raise ValueError("RADAR_SYNC_TOKEN must be at least 24 characters")
    server = ThreadingHTTPServer((config.listen_host, config.listen_port), make_handler(config))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def safe_artifact_path(path: str) -> PurePosixPath:
    target = PurePosixPath(path)
    if target.is_absolute() or ".." in target.parts or not target.parts or target.parts[0] != "YouTube Radar":
        raise ValueError("Unsafe artifact path")
    if target.suffix.lower() not in {".md", ".jpg"}:
        raise ValueError("Unsupported artifact type")
    return target


def sync_to_vault(base_url: str, token: str, vault: Path) -> int:
    parsed = urlparse(base_url)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and
        parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise ValueError("Use HTTPS for a remote server")
    if not token:
        raise ValueError("RADAR_SYNC_TOKEN is required")
    if not vault.is_dir():
        raise ValueError(f"Obsidian vault does not exist: {vault}")
    state_dir = vault / ".youtube-radar"
    state_dir.mkdir(exist_ok=True)
    state_file = state_dir / "state.json"
    cursor = json.loads(state_file.read_text()).get("revision", 0) if state_file.exists() else 0
    written = 0
    while True:
        payload = request_json(base_url.rstrip("/") + "/api/artifacts", token=token,
                               params={"after": str(cursor)}, timeout=60)
        artifacts = payload.get("artifacts") or []
        for artifact in artifacts:
            relative = safe_artifact_path(artifact["path"])
            target = vault.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            body = base64.b64decode(artifact["body_base64"], validate=True)
            temp = target.with_name(target.name + ".radar-tmp")
            temp.write_bytes(body)
            os.replace(temp, target)
            cursor = int(artifact["revision"])
            state_temp = state_file.with_suffix(".tmp")
            state_temp.write_text(json.dumps({"revision": cursor}))
            os.replace(state_temp, state_file)
            written += 1
        if not payload.get("has_more") or not artifacts:
            break
    return written

