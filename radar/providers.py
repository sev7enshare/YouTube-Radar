from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .http_client import request_json


TRANSCRIPT_API = "https://transcriptapi.com/api/v2"


@dataclass(frozen=True)
class Video:
    video_id: str
    channel_id: str
    channel_name: str
    title: str
    published_at: str
    description: str
    view_count: int | None


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def latest_videos(channel: str, api_key: str) -> list[Video]:
    data = request_json(f"{TRANSCRIPT_API}/youtube/channel/latest", token=api_key, params={"channel": channel})
    result: list[Video] = []
    top_channel = data.get("channel") or {}
    for item in data.get("results") or []:
        video_id = item.get("videoId")
        published = item.get("published")
        if not video_id or not published:
            continue
        parse_timestamp(published)
        raw_views = item.get("viewCount")
        views = int(raw_views) if raw_views is not None and str(raw_views).isdigit() else None
        result.append(Video(
            video_id=video_id,
            channel_id=item.get("channelId") or top_channel.get("channelId") or "",
            channel_name=item.get("author") or top_channel.get("title") or channel,
            title=item.get("title") or video_id,
            published_at=published,
            description=item.get("description") or "",
            view_count=views,
        ))
    return result


def transcript(video_id: str, api_key: str) -> tuple[list[dict[str, Any]], str]:
    data = request_json(
        f"{TRANSCRIPT_API}/youtube/transcript", token=api_key,
        params={"video_url": video_id, "format": "json", "include_timestamp": "true"},
    )
    segments = data.get("transcript")
    if not isinstance(segments, list) or not segments:
        raise ValueError("Transcript response contained no segments")
    cleaned = []
    for item in segments:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        cleaned.append({"text": item["text"].strip(), "start": float(item.get("start") or 0)})
    if not cleaned:
        raise ValueError("Transcript response contained no usable text")
    return cleaned, str(data.get("language") or "unknown")


def video_description(video_id: str, transcript_api_key: str, youtube_api_key: str = "") -> str:
    """Fetch the full description; the channel RSS feed often omits it."""
    if youtube_api_key:
        try:
            data = request_json("https://www.googleapis.com/youtube/v3/videos",
                                params={"key": youtube_api_key, "part": "snippet", "id": video_id})
            items = data.get("items") or []
            if items:
                return str((items[0].get("snippet") or {}).get("description") or "")
        except Exception:
            pass  # The existing TranscriptAPI source remains available as a fallback.
    data = request_json(
        f"{TRANSCRIPT_API}/youtube/video/metadata", token=transcript_api_key,
        params={"video_url": video_id}, timeout=65,
    )
    return str(data.get("description") or "")


def public_comments(video_id: str, api_key: str) -> list[dict[str, Any]]:
    """Sample both YouTube-relevant and recent public top-level comments."""
    found: dict[str, dict[str, Any]] = {}
    for order in ("relevance", "time"):
        data = request_json(
            "https://www.googleapis.com/youtube/v3/commentThreads",
            params={"key": api_key, "part": "snippet", "videoId": video_id,
                    "order": order, "maxResults": "100", "textFormat": "plainText"},
        )
        for item in data.get("items") or []:
            top = (item.get("snippet") or {}).get("topLevelComment") or {}
            detail = top.get("snippet") or {}
            comment_id = top.get("id")
            body = str(detail.get("textDisplay") or "").strip()
            if comment_id and body:
                found[comment_id] = {
                    "id": comment_id, "text": body,
                    "likes": int(detail.get("likeCount") or 0),
                    "published_at": str(detail.get("publishedAt") or ""),
                    "replies": int((item.get("snippet") or {}).get("totalReplyCount") or 0),
                    "author": str(detail.get("authorDisplayName") or ""),
                }
    return list(found.values())

