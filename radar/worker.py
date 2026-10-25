from __future__ import annotations

import json
import logging
import re
import sqlite3
import urllib.request
from hashlib import sha256
from datetime import datetime, timedelta

from .analysis import analyze
from .config import Config
from .db import (add_channel, connect, due_videos, finish_video, iso, processed_today,
                 put_artifact, record_discovery, retry_video, set_channel_error, utc_now)
from .http_client import HttpError
from .providers import latest_videos, parse_timestamp, transcript
from .render import (image_path, note_path, published_local, render_channel_index, render_index, render_note,
                     render_transcript, safe_component, transcript_path)


log = logging.getLogger(__name__)


def channel_folder(db: sqlite3.Connection, channel_id: int, channel_name: str) -> str:
    assigned = db.execute("SELECT artifact_base FROM videos WHERE channel_ref=? AND artifact_base IS NOT NULL LIMIT 1",
                          (channel_id,)).fetchone()
    if assigned:
        return assigned["artifact_base"].split("/", 2)[1]
    folder = safe_component(channel_name, 72)
    for other in db.execute("SELECT id, name FROM channels WHERE id<?", (channel_id,)):
        if safe_component(other["name"], 72) == folder:
            source = db.execute("SELECT url FROM channels WHERE id=?", (channel_id,)).fetchone()
            handle = safe_component(source["url"].rstrip("/").rsplit("/", 1)[-1].lstrip("@"), 32)
            folder += f" ({handle})"
            break
    return folder


def assign_artifact_base(db: sqlite3.Connection, row: sqlite3.Row) -> None:
    if row["artifact_base"]:
        return
    channel = channel_folder(db, row["channel_ref"], row["channel_name"])
    stem = f"{published_local(row):%Y-%m-%d}-{safe_component(row['title'], 120)}"
    base = f"YouTube Radar/{channel}/{stem}"
    number = 2
    while db.execute("SELECT 1 FROM videos WHERE artifact_base=? AND id!=?", (base, row["id"])).fetchone():
        base = f"YouTube Radar/{channel}/{stem} ({number})"
        number += 1
    db.execute("UPDATE videos SET artifact_base=? WHERE id=?", (base, row["id"]))


def retire_artifact(db: sqlite3.Connection, path: str, now: datetime) -> None:
    previous = db.execute("SELECT body, mime FROM artifacts WHERE path=? ORDER BY revision DESC LIMIT 1", (path,)).fetchone()
    if previous and previous["mime"] != "application/x-youtube-radar-delete":
        put_artifact(db, path, sha256(previous["body"]).hexdigest().encode(),
                     "application/x-youtube-radar-delete", now)


def migrate_legacy_artifacts(db: sqlite3.Connection, now: datetime) -> None:
    rows = db.execute("SELECT * FROM videos WHERE status='analyzed' AND artifact_base IS NULL").fetchall()
    for row in rows:
        old_note = f"YouTube Radar/notes/{parse_timestamp(row['published_at']).date().isoformat()}-{row['id']}.md"
        old_transcript = f"YouTube Radar/transcripts/{row['id']}.md"
        old_image = f"YouTube Radar/assets/{row['id']}.jpg"
        cover = db.execute("SELECT body FROM artifacts WHERE path=? AND mime='image/jpeg' ORDER BY revision DESC LIMIT 1",
                           (old_image,)).fetchone()
        with db:
            assign_artifact_base(db, row)
            fresh = db.execute("SELECT * FROM videos WHERE id=?", (row["id"],)).fetchone()
            segments = json.loads(row["transcript_json"])
            analysis = json.loads(row["analysis_json"])
            put_artifact(db, transcript_path(fresh), render_transcript(fresh, segments, row["language"]).encode(),
                         "text/markdown", now)
            if cover:
                put_artifact(db, image_path(fresh), cover["body"], "image/jpeg", now)
            note = render_note(fresh, analysis, row["language"], bool(cover), snapshots_for(db, row["id"]))
            put_artifact(db, note_path(fresh), note.encode(), "text/markdown", now)
            for path in (old_note, old_transcript, old_image):
                retire_artifact(db, path, now)
        log.info("Moved note into channel folder for %s", row["id"])


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
        assign_artifact_base(db, row)
        fresh = db.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
        put_artifact(db, transcript_path(fresh),
                     render_transcript(fresh, segments, language).encode(), "text/markdown", now)
        if cover:
            put_artifact(db, image_path(fresh), cover, "image/jpeg", now)
        note = render_note(fresh, analysis, language, bool(cover), snapshots_for(db, video_id))
        put_artifact(db, note_path(fresh), note.encode(), "text/markdown", now)
    log.info("Analyzed %s", video_id)


def refresh_notes(db: sqlite3.Connection, now: datetime) -> None:
    cutoff = iso(now - timedelta(hours=3))
    rows = db.execute("""SELECT * FROM videos WHERE status='analyzed' AND note_refreshed_at<?
        AND published_at>=?""", (cutoff, iso(now - timedelta(days=7)))).fetchall()
    with db:
        for row in rows:
            has_image = db.execute("SELECT 1 FROM artifacts WHERE path=? AND mime='image/jpeg' LIMIT 1",
                                   (image_path(row),)).fetchone() is not None
            note = render_note(row, json.loads(row["analysis_json"]), row["language"], has_image,
                               snapshots_for(db, row["id"]))
            put_artifact(db, note_path(row), note.encode(), "text/markdown", now)
            db.execute("UPDATE videos SET note_refreshed_at=? WHERE id=?", (iso(now), row["id"]))


def refresh_channel_indexes(db: sqlite3.Connection, now: datetime) -> None:
    channels = db.execute("SELECT * FROM channels WHERE url!='@manual' AND name IS NOT NULL ORDER BY id").fetchall()
    with db:
        for channel in channels:
            folder = channel_folder(db, channel["id"], channel["name"])
            videos = db.execute("""SELECT * FROM videos WHERE channel_ref=? AND status='analyzed'
                ORDER BY published_at DESC LIMIT 100""", (channel["id"],)).fetchall()
            put_artifact(db, f"YouTube Radar/{folder}/频道索引.md",
                         render_channel_index(channel["name"], channel["url"], videos).encode(),
                         "text/markdown", now)


def run_once(config: Config) -> dict[str, int]:
    config.require_worker_keys()
    db = connect(config.db_path)
    now = utc_now()
    counts = {"channels": 0, "new_videos": 0, "analyzed": 0, "errors": 0}
    try:
        migrate_legacy_artifacts(db, now)
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
        refresh_channel_indexes(db, now)
        recent = db.execute("""SELECT * FROM videos WHERE status!='ignored' AND published_at>=?
            ORDER BY published_at DESC LIMIT 100""", (iso(now - timedelta(days=7)),)).fetchall()
        with db:
            put_artifact(db, "YouTube Radar/最近视频.md", render_index(recent, now).encode(), "text/markdown", now)
        return counts
    finally:
        db.close()

