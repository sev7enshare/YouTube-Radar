from __future__ import annotations

import tempfile
import unittest
import base64
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from radar.analysis import classify_topics, select_valuable_comments
from radar.config import Config
from radar.db import add_channel, add_video, artifacts_after, connect, iso, put_artifact, record_discovery, utc_now
from radar.providers import Video, public_comments, video_description, video_duration_seconds, video_observations
from radar.http_client import HttpError
from radar.sync import safe_artifact_path, sync_to_vault
from radar.worker import assign_artifact_base, enrich_analyzed, refresh_notes, run_once


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
        self.assertTrue(add_video(db, old.video_id))
        self.assertEqual(db.execute("SELECT status FROM videos WHERE id=?", (old.video_id,)).fetchone()[0], "pending")
        self.assertFalse(add_video(db, old.video_id))
        db.close()

    def test_analyze_and_sync_into_vault(self) -> None:
        db = connect(self.config.db_path)
        add_video(db, "abcdefghijk")
        with db:
            db.execute("UPDATE videos SET channel_name=?, title=? WHERE id=?",
                       ("Sample 频道", "加密货币：本周走势？", "abcdefghijk"))
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
             patch("radar.worker.video_description", return_value="描述区原文：测试链接"), \
             patch("radar.worker.classify_topics", return_value=["加密货币走势"]):
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
        channel = vault / "YouTube Radar" / "Sample 频道"
        notes = list(channel.glob("*.md"))
        self.assertEqual(len(notes), 1)
        self.assertIn("加密货币 本周走势", notes[0].stem)
        self.assertNotIn("abcdefghijk", notes[0].name)
        text = notes[0].read_text()
        self.assertIn("这条视频讲了一个观点", text)
        self.assertIn("```mermaid", text)
        self.assertIn("Sample 频道", text)
        self.assertIn("描述区原文：测试链接", text)
        self.assertIn("[[YouTube Radar/主题/加密货币走势|加密货币走势]]", text)
        self.assertNotIn("/封面/", text)
        self.assertFalse((channel / "封面").exists())
        self.assertTrue((channel / "字幕" / (notes[0].stem + ".md")).exists())
        self.assertIn(f"[[YouTube Radar/Sample 频道/{notes[0].stem}\\|",
                      (vault / "YouTube Radar/最近视频.md").read_text())
        notes[0].write_text("我的复盘\n" + text)
        db = connect(self.config.db_path)
        with db:
            put_artifact(db, f"YouTube Radar/Sample 频道/{notes[0].name}",
                         (text + "\n自动更新\n").encode(), "text/markdown", utc_now())
        db.close()
        with patch("radar.sync.request_json", side_effect=fake_request):
            sync_to_vault("http://127.0.0.1:8765", self.config.sync_token, vault)
        self.assertIn("自动更新", notes[0].read_text())
        backups = list((vault / ".youtube-radar/backups/YouTube Radar/Sample 频道").glob("*.md"))
        self.assertEqual(len(backups), 1)
        self.assertIn("我的复盘", backups[0].read_text())
        with patch("radar.sync.request_json", side_effect=fake_request):
            self.assertEqual(sync_to_vault("http://127.0.0.1:8765", self.config.sync_token, vault), 0)

    def test_no_captions_uses_labeled_video_observations(self) -> None:
        config = replace(self.config, gemini_api_key="gemini-test")
        db = connect(config.db_path)
        add_video(db, "abcdefghijk")
        with db:
            db.execute("UPDATE videos SET channel_name=?, title=? WHERE id=?",
                       ("Sample 频道", "比特币走势", "abcdefghijk"))
        db.close()
        observations = [{"start": 0, "text": "博主介绍本周走势。"},
                        {"start": 35, "text": "画面出现价格图表。"},
                        {"start": 70, "text": "博主讨论风险。"}]
        analysis = {"summary": "本周走势分析。", "hook": "先提出问题。",
                    "sections": [], "claims": [], "angles": [], "limits": "近似内容记录。"}
        with patch("radar.worker.transcript", side_effect=HttpError(404, '{"code":"no_captions"}')), \
             patch("radar.worker.video_observations", return_value=(observations, "zh")) as fallback, \
             patch("radar.worker.video_duration_seconds", return_value=120), \
             patch("radar.worker.analyze", return_value=analysis), \
             patch("radar.worker.video_description", return_value="描述区"), \
             patch("radar.worker.classify_topics", return_value=[]):
            counts = run_once(config)
        self.assertEqual(counts["analyzed"], 1)
        fallback.assert_called_once_with("abcdefghijk", "gemini-test", "gemini-2.5-flash", None)
        db = connect(config.db_path)
        row = db.execute("SELECT source_kind, artifact_base FROM videos WHERE id=?", ("abcdefghijk",)).fetchone()
        note = db.execute("SELECT body FROM artifacts WHERE path=? ORDER BY revision DESC LIMIT 1",
                          (row["artifact_base"] + ".md",)).fetchone()[0].decode()
        record = db.execute("SELECT body FROM artifacts WHERE path LIKE '%/字幕/%.md' ORDER BY revision DESC LIMIT 1").fetchone()[0].decode()
        db.close()
        self.assertEqual(row["source_kind"], "gemini_video")
        self.assertIn("AI 视频内容记录", note)
        self.assertNotIn("**完整字幕：**", note)
        self.assertIn("并非逐字字幕", record)

    def test_no_captions_reads_later_video_sections(self) -> None:
        config = replace(self.config, gemini_api_key="gemini-test", youtube_api_key="youtube-test")
        db = connect(config.db_path)
        add_video(db, "abcdefghijk")
        db.close()
        first = ([{"start": 0, "text": "片头"}, {"start": 50, "text": "背景"},
                  {"start": 100, "text": "观点"}], "zh")
        middle = ([{"start": 100, "text": "中段"}, {"start": 400, "text": "论据"},
                   {"start": 700, "text": "转折"}], "zh")
        later = ([{"start": 760, "text": "后段"}, {"start": 900, "text": "回应"},
                  {"start": 1100, "text": "结尾"}], "zh")
        analysis = {"summary": "摘要", "hook": "钩子", "sections": [], "claims": [],
                    "angles": [], "limits": "近似记录"}
        with patch("radar.worker.transcript", side_effect=HttpError(404, '{"code":"no_captions"}')), \
             patch("radar.worker.video_duration_seconds", return_value=1200), \
             patch("radar.worker.video_observations", side_effect=[first, middle, later]) as fallback, \
             patch("radar.worker.analyze", return_value=analysis), \
             patch("radar.worker.video_description", return_value="描述区"), \
             patch("radar.worker.classify_topics", return_value=[]), \
             patch("radar.worker.public_comments", return_value=[]):
            counts = run_once(config)
        self.assertEqual(counts["analyzed"], 1)
        self.assertEqual(fallback.call_count, 3)
        db = connect(config.db_path)
        timeline = json.loads(db.execute("SELECT transcript_json FROM videos WHERE id=?",
                                         ("abcdefghijk",)).fetchone()[0])
        db.close()
        self.assertEqual(timeline[-1]["start"], 1100)

    def test_video_observations_rejects_short_output(self) -> None:
        response = {"candidates": [{"content": {"parts": [{"text": json.dumps({
            "segments": [{"start_minute": 0, "text": "一句话"}]})}]}}]}
        with patch("radar.providers.request_json", return_value=response):
            with self.assertRaisesRegex(ValueError, "too few"):
                video_observations("abcdefghijk", "key", "gemini-2.5-flash")

    def test_video_observations_converts_minutes_to_seconds(self) -> None:
        response = {"candidates": [{"content": {"parts": [{"text": json.dumps({
            "segments": [{"start_minute": 0, "text": "开始"},
                         {"start_minute": 1.5, "text": "中段"},
                         {"start_minute": 3, "text": "结尾"}], "language": "zh"})}]}}]}
        with patch("radar.providers.request_json", return_value=response):
            segments, language = video_observations("abcdefghijk", "key", "gemini-2.5-flash")
        self.assertEqual([item["start"] for item in segments], [0, 90, 180])
        self.assertEqual(language, "zh")

    def test_video_observations_drops_points_past_duration(self) -> None:
        response = {"candidates": [{"content": {"parts": [{"text": json.dumps({
            "segments": [{"start_minute": 0, "text": "开始"},
                         {"start_minute": 1, "text": "中段"},
                         {"start_minute": 2, "text": "结尾"},
                         {"start_minute": 20, "text": "超时长内容"}]})}]}}]}
        with patch("radar.providers.request_json", return_value=response):
            segments, _ = video_observations("abcdefghijk", "key", "gemini-2.5-flash", 180)
        self.assertEqual(len(segments), 3)

    def test_video_observations_recovers_from_malformed_json(self) -> None:
        malformed = {"candidates": [{"content": {"parts": [{"text": '{"segments": [bad'}]}}]}
        simple = {"candidates": [{"content": {"parts": [{"text": "0|开始\n1.5|中段\n3|结尾"}]}}]}
        with patch("radar.providers.request_json", side_effect=[malformed, simple]):
            segments, _ = video_observations("abcdefghijk", "key", "gemini-2.5-flash", 180)
        self.assertEqual([item["start"] for item in segments], [0, 90, 180])

    def test_video_observations_offsets_clipped_video(self) -> None:
        response = {"candidates": [{"content": {"parts": [{"text": json.dumps({
            "segments": [{"start_minute": 0, "text": "片段开头"},
                         {"start_minute": 1.5, "text": "片段中段"},
                         {"start_minute": 3, "text": "片段结尾"}]})}]}}]}
        with patch("radar.providers.request_json", return_value=response) as request:
            segments, _ = video_observations("abcdefghijk", "key", "gemini-2.5-flash",
                                             900, 600, 900)
        self.assertEqual([item["start"] for item in segments], [600, 690, 780])
        self.assertEqual(request.call_args.kwargs["payload"]["contents"][0]["parts"][1]["video_metadata"],
                         {"start_offset": "600s", "end_offset": "900s"})

    def test_migration_keeps_edited_legacy_note(self) -> None:
        db = connect(self.config.db_path)
        add_video(db, "abcdefghijk")
        now = utc_now()
        analysis = {"summary": "摘要", "hook": "钩子", "sections": [], "claims": [], "angles": [], "limits": "局限"}
        segments = [{"start": 0, "text": "字幕"}]
        with db:
            db.execute("""UPDATE videos SET status='analyzed', channel_name=?, title=?, published_at=?,
                transcript_json=?, analysis_json=?, language=?, analyzed_at=?, note_refreshed_at=? WHERE id=?""",
                ("Sample", "测试：主题", "2026-01-01T00:00:00+00:00", json.dumps(segments),
                 json.dumps(analysis), "zh", iso(now), iso(now), "abcdefghijk"))
            put_artifact(db, "YouTube Radar/notes/2026-01-01-abcdefghijk.md", b"old note", "text/markdown", now)
            put_artifact(db, "YouTube Radar/transcripts/abcdefghijk.md", b"old transcript", "text/markdown", now)
            put_artifact(db, "YouTube Radar/assets/abcdefghijk.jpg", b"old image", "image/jpeg", now)
        db.close()
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
            sync_to_vault("http://127.0.0.1:8765", self.config.sync_token, vault)
        old_note = vault / "YouTube Radar/notes/2026-01-01-abcdefghijk.md"
        old_note.write_text("my edited note")
        with patch("radar.worker.video_description", return_value=""), \
             patch("radar.worker.classify_topics", return_value=[]):
            run_once(self.config)
        with patch("radar.sync.request_json", side_effect=fake_request):
            sync_to_vault("http://127.0.0.1:8765", self.config.sync_token, vault)
        channel = vault / "YouTube Radar/Sample"
        notes = list(channel.glob("*.md"))
        self.assertEqual(len(notes), 1)
        self.assertIn("测试 主题", notes[0].stem)
        self.assertEqual(old_note.read_text(), "my edited note")
        self.assertFalse((vault / "YouTube Radar/transcripts/abcdefghijk.md").exists())
        self.assertFalse((vault / "YouTube Radar/assets/abcdefghijk.jpg").exists())

    def test_same_channel_date_and_title_get_distinct_names(self) -> None:
        db = connect(self.config.db_path)
        for video_id in ("abcdefghijk", "123456789ab"):
            add_video(db, video_id)
            with db:
                db.execute("UPDATE videos SET channel_name=?, title=?, published_at=? WHERE id=?",
                           ("Sample", "同名：选题", "2026-01-01T00:00:00+00:00", video_id))
            row = db.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
            with db:
                assign_artifact_base(db, row)
        bases = [r[0] for r in db.execute("SELECT artifact_base FROM videos ORDER BY id")]
        self.assertEqual(len(set(bases)), 2)
        self.assertTrue(any(base.endswith("同名 选题 (2)") for base in bases))
        self.assertTrue(all("Sample" in base for base in bases))
        db.close()

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
        home = db.execute("SELECT body FROM artifacts WHERE path=? ORDER BY revision DESC LIMIT 1",
                          ("YouTube Radar/Sample/频道索引.md",)).fetchone()
        self.assertIsNotNone(home)
        self.assertIn("尚无新视频笔记", home["body"].decode())
        db.close()

    def test_sync_rejects_unsafe_paths(self) -> None:
        for value in ("../other.md", "/tmp/other.md", "YouTube Radar/../other.md", "Other/a.md"):
            with self.assertRaises(ValueError):
                safe_artifact_path(value)

    def test_description_comments_and_cross_video_topic_links(self) -> None:
        db = connect(self.config.db_path)
        now = utc_now()
        base_analysis = {"summary": "比较订阅方法", "hook": "", "sections": [], "claims": [],
                         "angles": [], "limits": "仅依据字幕"}
        for video_id, channel in (("abcdefghijk", "频道甲"), ("123456789ab", "频道乙")):
            add_video(db, video_id)
            with db:
                db.execute("""UPDATE videos SET status='analyzed', channel_name=?, title=?,
                    analysis_json=?, language='zh', published_at=?, analyzed_at=?, note_refreshed_at=? WHERE id=?""",
                    (channel, channel + "的订阅方案", json.dumps(base_analysis, ensure_ascii=False),
                     iso(now - timedelta(days=2)), iso(now), iso(now), video_id))
            with db:
                assign_artifact_base(db, db.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone())
        config = replace(self.config, youtube_api_key="youtube-test-key")
        sample = [{"id": "comment.1", "text": "实际订阅时遇到支付失败，怎么解决？", "likes": 5,
                   "replies": 2, "published_at": iso(now), "author": "观众"}]
        selected = [{**sample[0], "category": "问题", "why": "暴露支付障碍"}]
        with patch("radar.worker.video_description", return_value="这是完整描述区。"), \
             patch("radar.worker.classify_topics", return_value=["AI订阅支付"]), \
             patch("radar.worker.public_comments", return_value=sample), \
             patch("radar.worker.select_valuable_comments", return_value=selected):
            counts = enrich_analyzed(db, config, now)
        self.assertEqual((counts["descriptions"], counts["topics"], counts["comments"]), (2, 2, 2))
        refresh_notes(db, now, force_all=True)
        notes = db.execute("""SELECT path,body FROM artifacts WHERE path LIKE 'YouTube Radar/%/%md'
            AND path NOT LIKE '%/字幕/%' ORDER BY revision""").fetchall()
        rendered = [row["body"].decode() for row in notes if "订阅方案" in row["path"]]
        self.assertEqual(len(rendered), 2)
        self.assertTrue(all("这是完整描述区" in text and "共同主题：AI订阅支付" in text
                            and "comment.1" in text for text in rendered))
        index = db.execute("SELECT body FROM artifacts WHERE path=? ORDER BY revision DESC LIMIT 1",
                           ("YouTube Radar/主题索引.md",)).fetchone()["body"].decode()
        self.assertIn("AI订阅支付", index)
        db.close()

    def test_crypto_channels_have_no_topic_section_or_cross_links(self) -> None:
        db = connect(self.config.db_path)
        now = utc_now()
        analysis = {"summary": "行情观点", "hook": "", "sections": [], "claims": [],
                    "angles": [], "limits": "以发布日期为准"}
        cases = (("abcdefghijk", "https://www.youtube.com/@tiabtc", "行情频道"),
                 ("123456789ab", "https://www.youtube.com/@sample", "普通频道"))
        for video_id, url, name in cases:
            add_video(db, video_id, url)
            with db:
                db.execute("""UPDATE videos SET status='analyzed', channel_name=?, title=?,
                    analysis_json=?, language='zh', published_at=?, analyzed_at=?, topics_json=? WHERE id=?""",
                    (name, name + "的比特币走势", json.dumps(analysis, ensure_ascii=False),
                     iso(now - timedelta(days=2)), iso(now), '["比特币走势"]', video_id))
                assign_artifact_base(db, db.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone())
        refresh_notes(db, now, force_all=True)
        bodies = {}
        for video_id, _, _ in cases:
            path = db.execute("SELECT artifact_base FROM videos WHERE id=?", (video_id,)).fetchone()[0] + ".md"
            bodies[video_id] = db.execute("SELECT body FROM artifacts WHERE path=? ORDER BY revision DESC LIMIT 1",
                                          (path,)).fetchone()[0].decode()
        self.assertNotIn("## 主题与相关视频", bodies["abcdefghijk"])
        self.assertIn("topics: []", bodies["abcdefghijk"])
        self.assertIn("## 主题与相关视频", bodies["123456789ab"])
        self.assertNotIn("行情频道", bodies["123456789ab"])
        topic_path = "YouTube Radar/主题/比特币走势.md"
        topic_note = db.execute("SELECT body FROM artifacts WHERE path=? ORDER BY revision DESC LIMIT 1",
                                (topic_path,)).fetchone()[0].decode()
        self.assertIn("普通频道", topic_note)
        self.assertNotIn("行情频道", topic_note)
        with db:
            db.execute("UPDATE videos SET topics_json=NULL WHERE id=?", ("abcdefghijk",))
        with patch("radar.worker.video_description", return_value=""), \
             patch("radar.worker.classify_topics", side_effect=AssertionError("Crypto topic model should not run")):
            counts = enrich_analyzed(db, self.config, now)
        self.assertEqual(counts["topics"], 0)
        db.close()

    def test_youtube_metadata_and_comment_sampling(self) -> None:
        calls = []

        def fake_request(url: str, **kwargs: object) -> dict:
            calls.append((url, kwargs["params"]))
            if url.endswith("/videos"):
                return {"items": [{"snippet": {"description": "完整描述和链接"}}]}
            comment = {"snippet": {"topLevelComment": {"id": "comment.1", "snippet": {
                "textDisplay": "我实际尝试后遇到支付失败，应该怎么处理？", "likeCount": 8,
                "publishedAt": "2026-10-26T00:00:00Z", "authorDisplayName": "观众"}},
                "totalReplyCount": 3}}
            return {"items": [comment]}

        with patch("radar.providers.request_json", side_effect=fake_request):
            self.assertEqual(video_description("abcdefghijk", "transcript-key", "youtube-key"),
                             "完整描述和链接")
            comments = public_comments("abcdefghijk", "youtube-key")
        self.assertEqual(len(comments), 1)
        self.assertEqual((comments[0]["likes"], comments[0]["replies"]), (8, 3))
        self.assertEqual([params["order"] for _, params in calls[1:]], ["relevance", "time"])

    def test_topic_and_comment_models_use_their_own_schema(self) -> None:
        def fake_request(_url: str, **kwargs: object) -> dict:
            instructions = kwargs["payload"]["instructions"]
            answer = ({"topics": ["AI订阅支付"]} if "主题索引" in instructions else
                      {"selected": [{"id": "comment.1", "category": "问题", "why": "支付障碍"}]})
            return {"output": [{"content": [{"type": "output_text",
                                              "text": json.dumps(answer, ensure_ascii=False)}]}]}

        comment = {"id": "comment.1", "text": "实际订阅时支付失败，这个问题怎么解决？",
                   "likes": 4, "replies": 1, "published_at": "2026-10-26T00:00:00Z"}
        with patch("radar.analysis.request_json", side_effect=fake_request):
            self.assertEqual(classify_topics(self.config, "标题", "摘要", "描述", []), ["AI订阅支付"])
            selected = select_valuable_comments(self.config, [comment])
        self.assertEqual(selected[0]["category"], "问题")

    def test_new_video_comments_are_refreshed_after_24_hours(self) -> None:
        db = connect(self.config.db_path)
        add_video(db, "abcdefghijk")
        now = utc_now()
        with db:
            db.execute("""UPDATE videos SET status='analyzed', published_at=?, analysis_json=?,
                metadata_checked_at=?, topics_json='[]' WHERE id=?""",
                (iso(now - timedelta(hours=4)), json.dumps({"summary": "摘要"}),
                 iso(now), "abcdefghijk"))
        config = replace(self.config, youtube_api_key="youtube-test-key")
        with patch("radar.worker.public_comments", return_value=[]), \
             patch("radar.worker.select_valuable_comments", return_value=[]):
            self.assertEqual(enrich_analyzed(db, config, now)["comments"], 1)
            self.assertEqual(db.execute("SELECT comments_round FROM videos").fetchone()[0], 1)
            self.assertEqual(enrich_analyzed(db, config, now + timedelta(hours=2))["comments"], 0)
            self.assertEqual(enrich_analyzed(db, config, now + timedelta(hours=21))["comments"], 1)
        self.assertEqual(db.execute("SELECT comments_round FROM videos").fetchone()[0], 2)
        db.close()


if __name__ == "__main__":
    unittest.main()

