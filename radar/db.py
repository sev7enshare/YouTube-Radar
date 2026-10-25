from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .providers import Video, parse_timestamp


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY,
            url TEXT UNIQUE NOT NULL,
            added_at TEXT NOT NULL,
            name TEXT,
            last_checked_at TEXT,
            last_error TEXT
        );
        CREATE TABLE IF NOT EXISTS videos (
            id TEXT PRIMARY KEY,
            channel_ref INTEGER NOT NULL REFERENCES channels(id),
            channel_name TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            published_at TEXT NOT NULL,
            view_count INTEGER,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT,
            last_error TEXT,
            language TEXT,
            transcript_json TEXT,
            analysis_json TEXT,
            analyzed_at TEXT,
            note_refreshed_at TEXT,
            artifact_base TEXT
        );
        CREATE INDEX IF NOT EXISTS videos_pending ON videos(status, next_attempt_at);
        CREATE TABLE IF NOT EXISTS snapshots (
            video_id TEXT NOT NULL REFERENCES videos(id),
            captured_at TEXT NOT NULL,
            view_count INTEGER NOT NULL,
            PRIMARY KEY(video_id, captured_at)
        );
        CREATE TABLE IF NOT EXISTS artifacts (
            revision INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            body BLOB NOT NULL,
            mime TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
    """)
    if "artifact_base" not in {row[1] for row in db.execute("PRAGMA table_info(videos)")}:
        db.execute("ALTER TABLE videos ADD COLUMN artifact_base TEXT")
    db.commit()
    return db


def add_channel(db: sqlite3.Connection, url: str) -> bool:
    url = url.strip()
    if not (url.startswith("@") or url.startswith("https://www.youtube.com/") or
            url.startswith("https://youtube.com/") or url.startswith("https://m.youtube.com/")):
        raise ValueError("Use a YouTube channel URL or @handle")
    with db:
        result = db.execute("INSERT OR IGNORE INTO channels(url, added_at) VALUES (?, ?)", (url, iso(utc_now())))
    return result.rowcount > 0


def add_video(db: sqlite3.Connection, video_id: str, channel_url: str = "@manual") -> bool:
    import re
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("Expected an 11-character YouTube video ID")
    add_channel(db, channel_url)
    channel = db.execute("SELECT id FROM channels WHERE url=?", (channel_url,)).fetchone()
    with db:
        result = db.execute("""INSERT OR IGNORE INTO videos
            (id, channel_ref, channel_name, title, published_at, status, next_attempt_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            (video_id, channel["id"], channel_url, video_id, iso(utc_now() - timedelta(hours=4)), iso(utc_now())))
        if result.rowcount:
            return True
        result = db.execute("""UPDATE videos SET status='pending', next_attempt_at=?, last_error=NULL
            WHERE id=? AND status IN ('ignored', 'unavailable')""", (iso(utc_now()), video_id))
    return result.rowcount > 0


def record_discovery(db: sqlite3.Connection, channel: sqlite3.Row, videos: list[Video], now: datetime) -> tuple[int, int]:
    new = 0
    ignored = 0
    added = parse_timestamp(channel["added_at"])
    with db:
        for video in videos:
            # A two-minute grace period handles clock skew, while avoiding paid backfills.
            is_new = parse_timestamp(video.published_at) >= added - timedelta(minutes=2)
            status = "pending" if is_new else "ignored"
            cur = db.execute("""INSERT OR IGNORE INTO videos
                (id, channel_ref, channel_name, title, description, published_at, view_count, status, next_attempt_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
                    video.video_id, channel["id"], video.channel_name, video.title,
                    video.description, iso(parse_timestamp(video.published_at)), video.view_count, status, iso(now),
                ))
            if cur.rowcount:
                new += int(is_new)
                ignored += int(not is_new)
            else:
                db.execute("""UPDATE videos SET title=?, description=?, view_count=?, channel_name=?
                    WHERE id=? AND channel_ref=?""", (
                        video.title, video.description, video.view_count, video.channel_name,
                        video.video_id, channel["id"],
                    ))
            if video.view_count is not None:
                db.execute("INSERT OR IGNORE INTO snapshots VALUES (?, ?, ?)",
                           (video.video_id, iso(now), video.view_count))
        db.execute("UPDATE channels SET name=?, last_checked_at=?, last_error=NULL WHERE id=?",
                   (videos[0].channel_name if videos else channel["name"], iso(now), channel["id"]))
    return new, ignored


def due_videos(db: sqlite3.Connection, now: datetime, age_hours: float, limit: int) -> list[sqlite3.Row]:
    cutoff = iso(now - timedelta(hours=age_hours))
    return db.execute("""SELECT * FROM videos WHERE status='pending' AND published_at<=?
        AND (next_attempt_at IS NULL OR next_attempt_at<=?)
        ORDER BY published_at ASC LIMIT ?""", (cutoff, iso(now), limit)).fetchall()


def processed_today(db: sqlite3.Connection, now: datetime) -> int:
    day = now.date().isoformat()
    return db.execute("SELECT COUNT(*) FROM videos WHERE analyzed_at>=?", (day,)).fetchone()[0]


def retry_video(db: sqlite3.Connection, video_id: str, reason: str, now: datetime,
                delay_minutes: int, terminal: bool = False) -> None:
    with db:
        db.execute("""UPDATE videos SET attempts=attempts+1, last_error=?, status=?, next_attempt_at=? WHERE id=?""",
                   (reason[:500], "unavailable" if terminal else "pending",
                    None if terminal else iso(now + timedelta(minutes=delay_minutes)), video_id))


def set_channel_error(db: sqlite3.Connection, channel_id: int, reason: str, now: datetime) -> None:
    with db:
        db.execute("UPDATE channels SET last_checked_at=?, last_error=? WHERE id=?",
                   (iso(now), reason[:500], channel_id))


def put_artifact(db: sqlite3.Connection, path: str, body: bytes, mime: str, now: datetime) -> None:
    previous = db.execute("SELECT body FROM artifacts WHERE path=? ORDER BY revision DESC LIMIT 1", (path,)).fetchone()
    if previous and previous["body"] == body:
        return
    db.execute("INSERT INTO artifacts(path, body, mime, created_at) VALUES (?, ?, ?, ?)",
               (path, body, mime, iso(now)))


def finish_video(db: sqlite3.Connection, video_id: str, language: str, analysis_json: str, now: datetime) -> None:
    db.execute("""UPDATE videos SET status='analyzed', language=?, analysis_json=?, analyzed_at=?,
        note_refreshed_at=?, last_error=NULL WHERE id=?""", (language, analysis_json, iso(now), iso(now), video_id))


def artifacts_after(db: sqlite3.Connection, revision: int, limit: int = 100) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM artifacts WHERE revision>? ORDER BY revision LIMIT ?", (revision, limit)).fetchall()

