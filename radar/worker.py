from __future__ import annotations

import json
import logging
import re
import sqlite3
import urllib.request
from datetime import datetime, timedelta

from .analysis import analyze
from .config import Config
from .db import (add_channel, connect, due_videos, finish_video, iso, processed_today,
                 put_artifact, record_discovery, retry_video, set_channel_error, utc_now)
from .http_client import HttpError
from .providers import latest_videos, parse_timestamp, transcript
from .render import note_path, render_index, render_note, render_transcript


log = logging.getLogger(__name__)


def thumbnail(video_id: str) -> bytes | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        return None
    request = urllib.request.Request(
        f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        headers={"User-Agent": "YouTubeRadar/0.1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if "image/jpeg" not in response.headers.get("Content-Type", ""):
                return None
            body = response.read(2_000_001)
            return body if len(body) <= 2_000_000 else None
    except Exception as exc:
        log.warning("Thumbnail unavailable for %s: %s", video_id, exc)
        return None


def snapshots_for(db: sqlite3.Connection, video_id: str) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM snapshots WHERE video_id=? ORDER BY captured_at", (video_id,)).fetchall()


def analyze_video(db: sqlite3.Connection, row: sqlite3.Row, config: Config, now: datetime) -> None:
    video_id = row["id"]
    if row["transcript_json"]:
        segments = json.loads(row["transcript_json"])
        language = row["language"] or "unknown"
    else:
        segments, language = transcript(video_id, config.transcript_api_key)
        with db:
            db.execute("UPDATE videos SET transcript_json=?, language=? WHERE id=?",
                       (json.dumps(segments, ensure_ascii=False), language, video_id))
    analysis = analyze(config, row["title"], segments)
    cover = thumbnail(video_id)
    with db:
        finish_video(db, video_id, language, json.dumps(analysis, ensure_ascii=False), now)
        fresh = db.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
        put_artifact(db, f"YouTube Radar/transcripts/{video_id}.md",
                     render_transcript(fresh, segments, language).encode(), "text/markdown", now)
        if cover:
            put_artifact(db, f"YouTube Radar/assets/{video_id}.jpg", cover, "image/jpeg", now)
        note = render_note(fresh, analysis, language, bool(cover), snapshots_for(db, video_id))
        put_artifact(db, note_path(fresh), note.encode(), "text/markdown", now)
    log.info("Analyzed %s", video_id)


def refresh_notes(db: sqlite3.Connection, now: datetime) -> None:
    cutoff = iso(now - timedelta(hours=3))
    rows = db.execute("""SELECT * FROM videos WHERE status='analyzed' AND note_refreshed_at<?
        AND published_at>=?""", (cutoff, iso(now - timedelta(days=7)))).fetchall()
    with db:
        for row in rows:
            has_image = db.execute("SELECT 1 FROM artifacts WHERE path=? LIMIT 1",
                                   (f"YouTube Radar/assets/{row['id']}.jpg",)).fetchone() is not None
            note = render_note(row, json.loads(row["analysis_json"]), row["language"], has_image,
                               snapshots_for(db, row["id"]))
            put_artifact(db, note_path(row), note.encode(), "text/markdown", now)
            db.execute("UPDATE videos SET note_refreshed_at=? WHERE id=?", (iso(now), row["id"]))


def run_once(config: Config) -> dict[str, int]:
    config.require_worker_keys()
    db = connect(config.db_path)
    now = utc_now()
    counts = {"channels": 0, "new_videos": 0, "analyzed": 0, "errors": 0}
    try:
        if config.channels_file.exists():
            for line in config.channels_file.read_text(encoding="utf-8").splitlines():
                value = line.strip()
                if value and not value.startswith("#"):
                    add_channel(db, value)
        channels = db.execute("SELECT * FROM channels WHERE url!='@manual' ORDER BY id").fetchall()
        for channel in channels:
            try:
                videos = latest_videos(channel["url"], config.transcript_api_key)
                new, _ = record_discovery(db, channel, videos, now)
                counts["new_videos"] += new
                counts["channels"] += 1
            except Exception as exc:
                counts["errors"] += 1
                set_channel_error(db, channel["id"], str(exc), now)
                log.exception("Channel check failed: %s", channel["url"])
        remaining = max(0, config.daily_video_limit - processed_today(db, now))
        for row in due_videos(db, now, config.process_after_hours, remaining):
            try:
                analyze_video(db, row, config, now)
                counts["analyzed"] += 1
            except HttpError as exc:
                counts["errors"] += 1
                age = now - parse_timestamp(row["published_at"])
                terminal = exc.status == 404 and age >= timedelta(hours=48)
                delay = exc.retry_after or (1440 if exc.status in (401, 402, 403) else
                                             min(240, 30 * 2 ** min(row["attempts"], 3)))
                retry_video(db, row["id"], str(exc), now, delay, terminal)
                log.warning("Transcript/LLM API error for %s: %s", row["id"], exc)
            except Exception as exc:
                counts["errors"] += 1
                retry_video(db, row["id"], str(exc), now, min(240, 30 * 2 ** min(row["attempts"], 3)))
                log.exception("Analysis failed for %s", row["id"])
        refresh_notes(db, now)
        recent = db.execute("""SELECT * FROM videos WHERE status!='ignored' AND published_at>=?
            ORDER BY published_at DESC LIMIT 100""", (iso(now - timedelta(days=7)),)).fetchall()
        with db:
            put_artifact(db, "YouTube Radar/最近视频.md", render_index(recent, now).encode(), "text/markdown", now)
        return counts
    finally:
        db.close()

