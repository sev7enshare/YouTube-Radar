from __future__ import annotations

import tempfile
import unittest
import base64
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from radar.config import Config
from radar.db import add_channel, add_video, artifacts_after, connect, iso, record_discovery, utc_now
from radar.providers import Video
from radar.sync import safe_artifact_path, sync_to_vault
from radar.worker import run_once


class RadarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = Config(
            db_path=self.root / "radar.sqlite3", transcript_api_key="test-key",
            llm_provider="openai", llm_api_key="test-key", llm_model="test-model",
            sync_token="a" * 32, poll_minutes=15, process_after_hours=3,
            daily_video_limit=20, listen_host="127.0.0.1", listen_port=0,
            channels_file=self.root / "channels.txt",
        )

    def test_existing_videos_are_baseline_only(self) -> None:
        db = connect(self.config.db_path)
        add_channel(db, "@sample")
        channel = db.execute("SELECT * FROM channels").fetchone()
        now = utc_now()
        old = Video("abcdefghijk", "UCtest", "Sample", "Old", iso(now - timedelta(days=1)), "", 100)
        new = Video("123456789ab", "UCtest", "Sample", "New", iso(now), "", 5)
        count, ignored = record_discovery(db, channel, [old, new], now)
        self.assertEqual((count, ignored), (1, 1))
        self.assertEqual(db.execute("SELECT status FROM videos WHERE id=?", (old.video_id,)).fetchone()[0], "ignored")
        self.assertEqual(db.execute("SELECT status FROM videos WHERE id=?", (new.video_id,)).fetchone()[0], "pending")
        db.close()

    def test_analyze_and_sync_into_vault(self) -> None:
        db = connect(self.config.db_path)
        add_video(db, "abcdefghijk")
        db.close()
        analysis = {
            "summary": "这条视频讲了一个观点。", "hook": "开头提出问题。",
            "sections": [{"title": "开头", "point": "提出问题", "start_seconds": 0}],
            "claims": [{"claim": "主张", "evidence": "视频中的说明", "start_seconds": 0}],
            "angles": [{"idea": "补充案例", "why": "提供不同证据"}], "limits": "仅依据字幕。",
        }
        segments = [{"start": 0.0, "text": "这是一个测试字幕。"}]
        with patch("radar.worker.transcript", return_value=(segments, "zh")), \
             patch("radar.worker.analyze", return_value=analysis), \
             patch("radar.worker.thumbnail", return_value=b"fake-jpeg"):
            counts = run_once(self.config)
        self.assertEqual(counts["analyzed"], 1)
        vault = self.root / "vault"
        vault.mkdir()
        def fake_request(_url: str, **kwargs: object) -> dict:
            db = connect(self.config.db_path)
            try:
                rows = artifacts_after(db, int(kwargs["params"]["after"]))
                return {"artifacts": [{"revision": r["revision"], "path": r["path"],
                         "mime": r["mime"], "body_base64": base64.b64encode(r["body"]).decode()}
                        for r in rows], "has_more": False}
            finally:
                db.close()
        with patch("radar.sync.request_json", side_effect=fake_request):
            written = sync_to_vault("http://127.0.0.1:8765", self.config.sync_token, vault)
        self.assertGreaterEqual(written, 4)
        notes = list((vault / "YouTube Radar" / "notes").glob("*.md"))
        self.assertEqual(len(notes), 1)
        text = notes[0].read_text()
        self.assertIn("这条视频讲了一个观点", text)
        self.assertIn("```mermaid", text)
        self.assertTrue((vault / "YouTube Radar" / "assets" / "abcdefghijk.jpg").exists())
        with patch("radar.sync.request_json", side_effect=fake_request):
            self.assertEqual(sync_to_vault("http://127.0.0.1:8765", self.config.sync_token, vault), 0)

    def test_channel_manifest_is_imported_without_backfill(self) -> None:
        self.config.channels_file.write_text("https://www.youtube.com/@sample\n")
        video = Video("abcdefghijk", "UCtest", "Sample", "Old",
                      iso(utc_now() - timedelta(days=1)), "", 100)
        with patch("radar.worker.latest_videos", return_value=[video]):
            counts = run_once(self.config)
        self.assertEqual(counts["channels"], 1)
        self.assertEqual(counts["new_videos"], 0)
        db = connect(self.config.db_path)
        self.assertEqual(db.execute("SELECT status FROM videos WHERE id=?", (video.video_id,)).fetchone()[0], "ignored")
        db.close()

    def test_sync_rejects_unsafe_paths(self) -> None:
        for value in ("../other.md", "/tmp/other.md", "YouTube Radar/../other.md", "Other/a.md"):
            with self.assertRaises(ValueError):
                safe_artifact_path(value)


if __name__ == "__main__":
    unittest.main()

