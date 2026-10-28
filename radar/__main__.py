from __future__ import annotations

import argparse
import logging
import os
import threading
from pathlib import Path

from .config import Config
from .db import add_channel, add_video, connect
from .providers import latest_videos
from .sync import start_server, sync_to_vault
from .worker import backfill_market_charts, backfill_market_views, run_once
from .market_data import record_market_days
from .db import utc_now


def main() -> None:
    parser = argparse.ArgumentParser(description="YouTube channel radar for Obsidian")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add-channel", help="Track a YouTube channel URL or @handle")
    add.add_argument("channel")
    manual = sub.add_parser("add-video", help="Analyze one video ID, including an older one")
    manual.add_argument("video_id")
    sub.add_parser("list", help="List tracked channels and recent videos")
    sub.add_parser("check-channels", help="Validate configured channels without running AI analysis")
    sub.add_parser("run-once", help="Check channels and process due videos")
    sub.add_parser("backfill-market-views", help="Extract market views from stored video text")
    sub.add_parser("backfill-market-charts", help="Collect spoken chart references and video timestamps")
    sub.add_parser("refresh-market-data", help="Record completed 24-hour Binance market windows")
    sub.add_parser("serve", help="Run the cloud worker and sync API")
    sync = sub.add_parser("sync", help="Pull notes into an Obsidian vault")
    sync.add_argument("--url", default=os.getenv("RADAR_SERVER_URL", "http://127.0.0.1:8765"))
    sync.add_argument("--vault", type=Path, default=Path(os.getenv("OBSIDIAN_VAULT", ".")))
    sync_loop = sub.add_parser("sync-loop", help="Keep a local Obsidian vault synchronized")
    sync_loop.add_argument("--url", default=os.getenv("RADAR_SERVER_URL", "http://127.0.0.1:8765"))
    sync_loop.add_argument("--vault", type=Path, default=Path(os.getenv("OBSIDIAN_VAULT", ".")))
    args = parser.parse_args()
    config = Config.from_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command in {"sync", "sync-loop"}:
        if args.command == "sync":
            print(f"Synced {sync_to_vault(args.url, config.sync_token, args.vault.expanduser())} files")
            return
        stop = threading.Event()
        try:
            while not stop.is_set():
                try:
                    count = sync_to_vault(args.url, config.sync_token, args.vault.expanduser())
                    logging.info("Synced %s artifacts", count)
                except Exception:
                    logging.exception("Sync failed; will retry")
                stop.wait(config.poll_minutes * 60)
        except KeyboardInterrupt:
            pass
        return
    if args.command == "check-channels":
        if not config.transcript_api_key:
            parser.error("TRANSCRIPT_API_KEY is required")
        if not config.channels_file.exists():
            parser.error(f"Channels file not found: {config.channels_file}")
        failed = False
        for raw in config.channels_file.read_text(encoding="utf-8").splitlines():
            channel = raw.strip()
            if not channel or channel.startswith("#"):
                continue
            try:
                items = latest_videos(channel, config.transcript_api_key)
                print(f"OK {channel} | {items[0].channel_name if items else 'no videos'} | {len(items)} recent videos")
            except Exception as exc:
                failed = True
                print(f"ERROR {channel} | {exc}")
        if failed:
            raise SystemExit(1)
        return
    if args.command in {"add-channel", "add-video", "list"}:
        db = connect(config.db_path)
        try:
            if args.command == "add-channel":
                print("Added" if add_channel(db, args.channel) else "Already tracked")
            elif args.command == "add-video":
                print("Queued" if add_video(db, args.video_id) else "Already queued")
            else:
                for row in db.execute("SELECT * FROM channels ORDER BY id"):
                    print(f"CHANNEL {row['url']} | {row['name'] or 'unknown'} | error={row['last_error'] or '-'}")
                for row in db.execute("SELECT * FROM videos WHERE status!='ignored' ORDER BY published_at DESC LIMIT 30"):
                    print(f"VIDEO {row['id']} | {row['status']} | {row['title']} | error={row['last_error'] or '-'}")
        finally:
            db.close()
        return
    if args.command == "run-once":
        print(run_once(config))
        return
    if args.command == "backfill-market-views":
        config.require_worker_keys()
        completed, failed = backfill_market_views(config)
        print(f"Market views: {completed} completed, {failed} failed")
        if failed:
            raise SystemExit(1)
        return
    if args.command == "backfill-market-charts":
        completed, failed = backfill_market_charts(config)
        print(f"Chart inspections: {completed} completed, {failed} deferred")
        return
    if args.command == "refresh-market-data":
        db = connect(config.db_path)
        try:
            from .worker import refresh_notes
            count = record_market_days(db, utc_now())
            refresh_notes(db, utc_now(), force_all=True)
            print(f"Market windows recorded: {count}")
        finally:
            db.close()
        return
    config.require_worker_keys()
    server = start_server(config)
    logging.info("Sync API listening at %s:%s", config.listen_host, config.listen_port)
    stop = threading.Event()
    try:
        while not stop.is_set():
            logging.info("Cycle: %s", run_once(config))
            stop.wait(config.poll_minutes * 60)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()

