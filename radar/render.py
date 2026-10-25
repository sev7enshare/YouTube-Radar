from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .analysis import stamp, transcript_lines
from .providers import parse_timestamp


def safe_text(value: Any) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()


def yaml_quote(value: Any) -> str:
    return json.dumps(safe_text(value), ensure_ascii=False)


def link(video_id: str, seconds: float) -> str:
    return f"https://www.youtube.com/watch?v={video_id}&t={max(0, int(seconds))}s"


def safe_component(value: Any, max_bytes: int = 120) -> str:
    """Make a readable, portable folder or file name from a channel/title."""
    cleaned = "".join(char if char.isalnum() or char in " -_" else " " for char in safe_text(value))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-_")
    result = ""
    for char in cleaned:
        if len((result + char).encode("utf-8")) > max_bytes:
            break
        result += char
    return result.rstrip(" .-_") or "未命名"


def published_local(row: sqlite3.Row) -> datetime:
    return parse_timestamp(row["published_at"]).astimezone(timezone(timedelta(hours=8)))


def artifact_base(row: sqlite3.Row) -> str:
    value = row["artifact_base"]
    if not value:
        raise ValueError(f"Artifact path not assigned for {row['id']}")
    return str(value)


def note_path(row: sqlite3.Row) -> str:
    return artifact_base(row) + ".md"


def transcript_path(row: sqlite3.Row) -> str:
    base = artifact_base(row)
    folder, stem = base.rsplit("/", 1)
    return f"{folder}/字幕/{stem}.md"


def image_path(row: sqlite3.Row) -> str:
    base = artifact_base(row)
    folder, stem = base.rsplit("/", 1)
    return f"{folder}/封面/{stem}.jpg"


def render_transcript(row: sqlite3.Row, segments: list[dict[str, Any]], language: str) -> str:
    lines = [f"# {safe_text(row['title'])} · 字幕", "",
             f"频道：{safe_text(row['channel_name'])}", f"来源：{link(row['id'], 0)}", f"语言：{language}", "",
             "> [!warning] 字幕可能由语音识别生成，具体表述请回看原视频。", ""]
    for segment in segments:
        if segment["text"]:
            lines.append(f"[{stamp(float(segment['start']))}]({link(row['id'], segment['start'])}) " + safe_text(segment["text"]))
    return "\n".join(lines) + "\n"


def render_note(row: sqlite3.Row, analysis: dict[str, Any], language: str,
                has_image: bool, snapshots: list[sqlite3.Row]) -> str:
    video_id = row["id"]
    title = safe_text(row["title"])
    lines = ["---", f"title: {yaml_quote(title)}", f"channel: {yaml_quote(row['channel_name'])}",
             f"published: {yaml_quote(row['published_at'])}", f"video_id: {yaml_quote(video_id)}",
             f"language: {yaml_quote(language)}", "tags: [youtube-radar]", "---", "",
             f"# {title}", ""]
    if has_image:
        lines += [f"![[{image_path(row)}]]", ""]
    lines += [f"**频道：** {safe_text(row['channel_name'])}  ",
              f"**发布时间（北京时间）：** {published_local(row):%Y-%m-%d %H:%M}  ",
              f"**原视频：** [打开 YouTube]({link(video_id, 0)})  ",
              f"**完整字幕：** [[{transcript_path(row).removesuffix('.md')}|点击查看]]", "",
              "## 一眼看懂", "", safe_text(analysis.get("summary")), "",
              "## 内容结构", ""]
    sections = analysis.get("sections") or []
    if sections:
        lines += ["```mermaid", "flowchart TD"]
        for index, section in enumerate(sections[:12]):
            label = safe_text(section.get("title")).replace('"', "'")[:45]
            lines.append(f'  S{index}["{label}"]')
            if index:
                lines.append(f"  S{index - 1} --> S{index}")
        lines += ["```", ""]
        for section in sections:
            seconds = section.get("start_seconds", 0)
            lines.append(f"- [{stamp(seconds)}]({link(video_id, seconds)}) **{safe_text(section.get('title'))}**：{safe_text(section.get('point'))}")
    else:
        lines.append("字幕中未提取出可靠的章节时间点。")
    lines += ["", "## 开头钩子", "", safe_text(analysis.get("hook")), "",
              "## 关键观点与依据", ""]
    for claim in (analysis.get("claims") or [])[:15]:
        seconds = claim.get("start_seconds", 0)
        lines.append(f"- [{stamp(seconds)}]({link(video_id, seconds)}) **{safe_text(claim.get('claim'))}**：{safe_text(claim.get('evidence'))}")
    lines += ["", "## 我的差异化选题机会", ""]
    for angle in (analysis.get("angles") or [])[:8]:
        lines.append(f"- **{safe_text(angle.get('idea'))}**：{safe_text(angle.get('why'))}")
    lines += ["", "## 播放数据快照", "", "| 采集时间 UTC | 播放量 |", "|---|---:|"]
    for snapshot in snapshots[-20:]:
        lines.append(f"| {snapshot['captured_at']} | {snapshot['view_count']:,} |")
    lines += ["", "> [!note] 热度判断", "> 播放量需与同频道、同视频类型、相同发布时长的数据比较。积累足够同期快照后再给出相对热度。", "",
              "## 资料局限", "", safe_text(analysis.get("limits")) or "仅依据字幕，未核验画面内容。", ""]
    return "\n".join(lines)


def render_index(videos: list[sqlite3.Row], generated_at: datetime) -> str:
    lines = ["# YouTube Radar · 最近视频", "",
             "| 视频 | 频道 | 发布时间 UTC | 状态 | 当前播放量 |", "|---|---|---|---|---:|"]
    for row in videos:
        title = safe_text(row["title"]).replace("|", "\\|")[:80]
        channel = safe_text(row["channel_name"]).replace("|", "\\|")
        target = f"[[{note_path(row).removesuffix('.md')}\\|{title}]]" if row["status"] == "analyzed" else f"[{title}]({link(row['id'], 0)})"
        views = f"{row['view_count']:,}" if row["view_count"] is not None else "—"
        lines.append(f"| {target} | {channel} | {row['published_at']} | {row['status']} | {views} |")
    lines += ["", "状态：pending=等待处理，analyzed=已分析，unavailable=字幕不可用，ignored=加入频道前的旧视频。", ""]
    return "\n".join(lines)

