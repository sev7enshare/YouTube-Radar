from __future__ import annotations

import json
import logging
import sqlite3
from hashlib import sha256
from datetime import datetime, timedelta

from .analysis import analyze, classify_topics, select_valuable_comments
from .config import Config
from .db import (add_channel, connect, due_videos, finish_video, iso, processed_today,
                 put_artifact, record_discovery, retry_video, set_channel_error, utc_now)
from .http_client import HttpError
from .providers import (latest_videos, parse_timestamp, public_comments, transcript, video_description,
                        video_duration_seconds, video_observations)
from .render import (note_path, published_local, render_channel_index, render_index, render_note,
                     render_topic_index, render_topic_note, render_transcript, safe_component, transcript_path,
                     topic_path)


log = logging.getLogger(__name__)

NO_TOPIC_CHANNEL_URLS = (
    "https://www.youtube.com/@tiabtc",
    "https://www.youtube.com/@CakeBaBa",
    "https://www.youtube.com/@Traderfengge",
)


def no_topic_channel_ids(db: sqlite3.Connection) -> set[int]:
    placeholders = ",".join("?" for _ in NO_TOPIC_CHANNEL_URLS)
    return {row["id"] for row in db.execute(
        f"SELECT id FROM channels WHERE url IN ({placeholders})", NO_TOPIC_CHANNEL_URLS)}


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


def retire_generated_covers(db: sqlite3.Connection, now: datetime) -> int:
    """Send image removals to synced vaults, then discard stored JPEG bodies."""
    paths = [row[0] for row in db.execute("SELECT DISTINCT path FROM artifacts WHERE mime='image/jpeg'")]
    if not paths:
        return 0
    with db:
        for path in paths:
            retire_artifact(db, path, now)
        db.execute("DELETE FROM artifacts WHERE mime='image/jpeg'")
    return len(paths)


def migrate_legacy_artifacts(db: sqlite3.Connection, now: datetime) -> None:
    rows = db.execute("SELECT * FROM videos WHERE status='analyzed' AND artifact_base IS NULL").fetchall()
    for row in rows:
        old_note = f"YouTube Radar/notes/{parse_timestamp(row['published_at']).date().isoformat()}-{row['id']}.md"
        old_transcript = f"YouTube Radar/transcripts/{row['id']}.md"
        old_image = f"YouTube Radar/assets/{row['id']}.jpg"
        with db:
            assign_artifact_base(db, row)
            fresh = db.execute("SELECT * FROM videos WHERE id=?", (row["id"],)).fetchone()
            segments = json.loads(row["transcript_json"])
            analysis = json.loads(row["analysis_json"])
            put_artifact(db, transcript_path(fresh), render_transcript(fresh, segments, row["language"]).encode(),
                         "text/markdown", now)
            note = render_note(fresh, analysis, row["language"], snapshots_for(db, row["id"]),
                               include_topics=row["channel_ref"] not in no_topic_channel_ids(db))
            put_artifact(db, note_path(fresh), note.encode(), "text/markdown", now)
            for path in (old_note, old_transcript, old_image):
                retire_artifact(db, path, now)
        log.info("Moved note into channel folder for %s", row["id"])


def snapshots_for(db: sqlite3.Connection, video_id: str) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM snapshots WHERE video_id=? ORDER BY captured_at", (video_id,)).fetchall()


def analyze_video(db: sqlite3.Connection, row: sqlite3.Row, config: Config, now: datetime) -> None:
    video_id = row["id"]
    source_kind = row["source_kind"] or "captions"
    video_duration = None
    if row["transcript_json"]:
        segments = json.loads(row["transcript_json"])
        language = row["language"] or "unknown"
    else:
        try:
            segments, language = transcript(video_id, config.transcript_api_key)
        except HttpError as exc:
            if exc.status != 404 or '"no_captions"' not in str(exc) or not config.gemini_api_key:
                raise
            if config.youtube_api_key:
                try:
                    video_duration = video_duration_seconds(video_id, config.youtube_api_key)
                except Exception as duration_error:
                    log.warning("Video duration unavailable for %s: %s", video_id, duration_error)
            segments, language = video_observations(video_id, config.gemini_api_key,
                                                    config.gemini_video_model, video_duration)
            if video_duration and video_duration >= 600:
                for _ in range(3):
                    latest = max(item["start"] for item in segments)
                    if latest >= video_duration * 0.9:
                        break
                    clip_start = max(0, int(latest) - 60)
                    clip_end = min(video_duration, clip_start + 720)
                    if clip_end <= latest + 30:
                        break
                    try:
                        more, _ = video_observations(video_id, config.gemini_api_key,
                                                     config.gemini_video_model, video_duration,
                                                     clip_start, clip_end)
                    except Exception as clip_error:
                        log.warning("Later video section unavailable for %s: %s", video_id, clip_error)
                        break
                    segments.extend(more)
                    segments.sort(key=lambda item: item["start"])
            source_kind = "gemini_video"
        with db:
            db.execute("UPDATE videos SET transcript_json=?, language=?, source_kind=? WHERE id=?",
                       (json.dumps(segments, ensure_ascii=False), language, source_kind, video_id))
    analysis = analyze(config, row["title"], segments, source_kind)
    if source_kind == "gemini_video" and video_duration and max(item["start"] for item in segments) < video_duration * 0.9:
        analysis["limits"] = (str(analysis.get("limits") or "").strip() +
                              " AI 内容记录可能未覆盖视频结尾，请打开原视频核对。").strip()
    with db:
        finish_video(db, video_id, language, json.dumps(analysis, ensure_ascii=False), now)
        assign_artifact_base(db, row)
        fresh = db.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
        put_artifact(db, transcript_path(fresh),
                     render_transcript(fresh, segments, language).encode(), "text/markdown", now)
        note = render_note(fresh, analysis, language, snapshots_for(db, video_id),
                           include_topics=row["channel_ref"] not in no_topic_channel_ids(db))
        put_artifact(db, note_path(fresh), note.encode(), "text/markdown", now)
    log.info("Analyzed %s", video_id)


def enrich_analyzed(db: sqlite3.Connection, config: Config, now: datetime) -> dict[str, int]:
    """Fill optional sources independently so outages never discard the core analysis."""
    counts = {"descriptions": 0, "topics": 0, "comments": 0}
    rows = db.execute("SELECT * FROM videos WHERE status='analyzed' ORDER BY published_at").fetchall()
    no_topics = no_topic_channel_ids(db)
    catalog: list[str] = []
    for row in rows:
        if row["channel_ref"] not in no_topics and row["topics_json"]:
            for name in json.loads(row["topics_json"]):
                if name not in catalog:
                    catalog.append(name)
    for row in rows:
        video_id = row["id"]
        if not row["metadata_checked_at"] and (not row["metadata_next_attempt_at"] or
                                                  row["metadata_next_attempt_at"] <= iso(now)):
            try:
                description = video_description(video_id, config.transcript_api_key, config.youtube_api_key)
                with db:
                    db.execute("""UPDATE videos SET description=CASE WHEN ?!='' THEN ? ELSE description END,
                        metadata_checked_at=?, metadata_next_attempt_at=NULL WHERE id=?""",
                        (description, description, iso(now), video_id))
                counts["descriptions"] += 1
            except Exception as exc:
                with db:
                    db.execute("UPDATE videos SET metadata_next_attempt_at=? WHERE id=?",
                               (iso(now + timedelta(hours=1)), video_id))
                log.warning("Description fetch deferred for %s: %s", video_id, exc)
        fresh = db.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
        if fresh["channel_ref"] not in no_topics and fresh["topics_json"] is None and (not fresh["topics_next_attempt_at"] or
                                              fresh["topics_next_attempt_at"] <= iso(now)):
            try:
                analysis = json.loads(fresh["analysis_json"])
                topics = classify_topics(config, fresh["title"], analysis.get("summary", ""),
                                         fresh["description"], catalog)
                with db:
                    db.execute("UPDATE videos SET topics_json=?, topics_next_attempt_at=NULL WHERE id=?",
                               (json.dumps(topics, ensure_ascii=False), video_id))
                for name in topics:
                    if name not in catalog:
                        catalog.append(name)
                counts["topics"] += 1
            except Exception as exc:
                with db:
                    db.execute("UPDATE videos SET topics_next_attempt_at=? WHERE id=?",
                               (iso(now + timedelta(hours=1)), video_id))
                log.warning("Topic classification deferred for %s: %s", video_id, exc)
        if not config.youtube_api_key:
            continue
        fresh = db.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
        due_second = fresh["comments_round"] == 1 and now >= parse_timestamp(fresh["published_at"]) + timedelta(hours=24)
        if (fresh["comments_round"] == 0 or due_second) and (not fresh["comments_next_attempt_at"] or
                                                               fresh["comments_next_attempt_at"] <= iso(now)):
            try:
                sample = public_comments(video_id, config.youtube_api_key)
                selected = select_valuable_comments(config, sample)
                round_number = 2 if now >= parse_timestamp(fresh["published_at"]) + timedelta(hours=24) else 1
                with db:
                    db.execute("""UPDATE videos SET comments_json=?, comments_round=?,
                        comments_next_attempt_at=NULL, comments_error=NULL WHERE id=?""",
                        (json.dumps(selected, ensure_ascii=False), round_number, video_id))
                counts["comments"] += 1
            except HttpError as exc:
                disabled = exc.status == 403 and "commentsDisabled" in str(exc)
                with db:
                    db.execute("""UPDATE videos SET comments_json=CASE WHEN ? THEN '[]' ELSE comments_json END,
                        comments_round=CASE WHEN ? THEN 2 ELSE comments_round END,
                        comments_next_attempt_at=?, comments_error=? WHERE id=?""",
                        (disabled, disabled, None if disabled else iso(now + timedelta(hours=1)), str(exc)[:500], video_id))
                log.warning("Comment fetch deferred for %s: %s", video_id, exc)
            except Exception as exc:
                with db:
                    db.execute("UPDATE videos SET comments_next_attempt_at=?, comments_error=? WHERE id=?",
                               (iso(now + timedelta(hours=1)), str(exc)[:500], video_id))
                log.warning("Comment fetch deferred for %s: %s", video_id, exc)
    return counts


def refresh_notes(db: sqlite3.Connection, now: datetime, force_all: bool = False) -> None:
    rows = db.execute("SELECT * FROM videos WHERE status='analyzed' AND artifact_base IS NOT NULL").fetchall()
    no_topics = no_topic_channel_ids(db)
    topics_by_id = {row["id"]: (json.loads(row["topics_json"] or "[]")
                                if row["channel_ref"] not in no_topics else []) for row in rows}
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        for name in topics_by_id[row["id"]]:
            groups.setdefault(name, []).append(row)
    with db:
        for row in rows:
            if not force_all and (row["published_at"] < iso(now - timedelta(days=7)) or
                                  (row["note_refreshed_at"] and
                                   row["note_refreshed_at"] >= iso(now - timedelta(hours=3)))):
                continue
            own_topics = topics_by_id[row["id"]]
            related = []
            for other in rows:
                if other["id"] == row["id"]:
                    continue
                shared = [name for name in own_topics if name in topics_by_id[other["id"]]]
                if shared:
                    related.append((note_path(other), other["title"], shared))
            related.sort(key=lambda item: item[0], reverse=True)
            comments = json.loads(row["comments_json"]) if row["comments_json"] is not None else None
            note = render_note(row, json.loads(row["analysis_json"]), row["language"],
                               snapshots_for(db, row["id"]), own_topics, related, comments,
                               include_topics=row["channel_ref"] not in no_topics)
            put_artifact(db, note_path(row), note.encode(), "text/markdown", now)
            db.execute("UPDATE videos SET note_refreshed_at=? WHERE id=?", (iso(now), row["id"]))
        put_artifact(db, "YouTube Radar/主题索引.md", render_topic_index(groups).encode(), "text/markdown", now)
        for name, videos in groups.items():
            put_artifact(db, topic_path(name), render_topic_note(name, videos).encode(), "text/markdown", now)
        active_paths = {topic_path(name) for name in groups}
        stale_paths = [row[0] for row in db.execute(
            "SELECT DISTINCT path FROM artifacts WHERE path LIKE 'YouTube Radar/主题/%.md' AND mime='text/markdown'")]
        for path in stale_paths:
            if path not in active_paths:
                retire_artifact(db, path, now)


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
    counts = {"channels": 0, "new_videos": 0, "analyzed": 0, "errors": 0, "covers_removed": 0,
              "descriptions": 0, "topics": 0, "comments": 0}
    try:
        migrate_legacy_artifacts(db, now)
        counts["covers_removed"] = retire_generated_covers(db, now)
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
        counts.update(enrich_analyzed(db, config, now))
        refresh_notes(db, now, force_all=any(counts[key] for key in
                      ("analyzed", "descriptions", "topics", "comments", "covers_removed")))
        refresh_channel_indexes(db, now)
        recent = db.execute("""SELECT * FROM videos WHERE status!='ignored' AND published_at>=?
            ORDER BY published_at DESC LIMIT 100""", (iso(now - timedelta(days=7)),)).fetchall()
        with db:
            put_artifact(db, "YouTube Radar/最近视频.md", render_index(recent, now).encode(), "text/markdown", now)
        return counts
    finally:
        db.close()

