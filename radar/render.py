from __future__ import annotations

import json
import re
import sqlite3
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from .analysis import stamp, transcript_lines
from .market_data import strict_price, symbol_for
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


def topic_path(name: str) -> str:
    return f"YouTube Radar/主题/{safe_component(name, 72)}.md"


def source_quote(value: str) -> list[str]:
    """Keep source text visible without letting it create vault embeds or HTML."""
    cleaned = value.replace("<", "&lt;").replace("![[", r"\!\[\[").replace("[[", r"\[\[")
    return ["> " + line for line in cleaned.splitlines()]


def render_transcript(row: sqlite3.Row, segments: list[dict[str, Any]], language: str) -> str:
    generated = row["source_kind"] == "gemini_video"
    lines = [f"# {safe_text(row['title'])} · {'AI 视频内容记录' if generated else '字幕'}", "",
             f"频道：{safe_text(row['channel_name'])}", f"来源：{link(row['id'], 0)}", f"语言：{language}", "",
             ("> [!warning] 这是 AI 根据语音和画面生成的近似内容记录，并非逐字字幕；时间点和交易数字请回看原视频。"
              if generated else "> [!warning] 字幕可能由语音识别生成，具体表述请回看原视频。"), ""]
    for segment in segments:
        if segment["text"]:
            lines.append(f"[{stamp(float(segment['start']))}]({link(row['id'], segment['start'])}) " + safe_text(segment["text"]))
    return "\n".join(lines) + "\n"


def render_note(row: sqlite3.Row, analysis: dict[str, Any], language: str,
                snapshots: list[sqlite3.Row], topics: list[str] | None = None,
                related: list[tuple[str, str, list[str]]] | None = None,
                comments: list[dict[str, Any]] | None = None,
                include_topics: bool = True, market_mode: bool = False,
                market_days: list[sqlite3.Row] | None = None) -> str:
    video_id = row["id"]
    title = safe_text(row["title"])
    topics = topics or []
    lines = ["---", f"title: {yaml_quote(title)}", f"channel: {yaml_quote(row['channel_name'])}",
             f"published: {yaml_quote(row['published_at'])}", f"video_id: {yaml_quote(video_id)}",
             f"language: {yaml_quote(language)}", f"source_kind: {yaml_quote(row['source_kind'])}", "tags: [youtube-radar]",
             "topics: [" + ", ".join(yaml_quote(topic) for topic in topics) + "]", "---", "",
             f"# {title}", ""]
    lines += [f"**频道：** {safe_text(row['channel_name'])}  ",
              f"**发布时间（北京时间）：** {published_local(row):%Y-%m-%d %H:%M}  ",
              f"**原视频：** [打开 YouTube]({link(video_id, 0)})  ",
              f"**{'AI 视频内容记录' if row['source_kind'] == 'gemini_video' else '完整字幕'}：** [[{transcript_path(row).removesuffix('.md')}|点击查看]]", ""]
    if market_mode:
        view = analysis.get("market_view") or {}
        lines += ["## 本期市场观点", "", "> [!warning] 博主原话与模型归纳分开记录；模型归纳不是事实核验或交易建议。" +
                  safe_text(view.get("source_warning") or "观点提取尚未完成；交易前核对原视频。"), ""]
        positions = view.get("positions") or []
        if view.get("version") != 2:
            lines += ["旧版观点缺少逐字证据校验，等待重新提取；以下旧版归纳不作为交易依据。", ""]
        else:
            for item in positions:
                sources = item.get("sources") or []
                if not sources:
                    continue
                seconds = sources[0]["start_seconds"]
                lines += [f"### {safe_text(item.get('asset'))} · {safe_text(item.get('stance'))} · {safe_text(item.get('horizon'))}", "",
                          f"- 博主原话：[原视频 {stamp(seconds)}]({link(video_id, seconds)})「{safe_text(item.get('quote'))}」",
                          f"- 模型归纳：{safe_text(item.get('view'))}"]
                for field, label in (("condition", "成立条件（模型归纳）"),
                                     ("invalidation", "失效条件（模型归纳）")):
                    if item.get(field) and item.get(field + "_quote"):
                        at = item.get(field + "_source_seconds", seconds)
                        lines.append(f"- {label}：{safe_text(item[field])}；"
                                     f"[原话 {stamp(at)}]({link(video_id, at)})「{safe_text(item[field + '_quote'])}」")
                lines.append("")
            if not positions:
                lines += ["本期没有提取到可逐字核对的明确方向判断。", ""]
            for item in (view.get("statements") or [])[:8]:
                sources = item.get("sources") or []
                if sources:
                    seconds = sources[0]["start_seconds"]
                    lines.append(f"- 无明确方向的观察 · {safe_text(item.get('asset'))}："
                                 f"[原视频 {stamp(seconds)}]({link(video_id, seconds)})「{safe_text(item.get('quote'))}」")
            if view.get("statements"):
                lines.append("")
        if view.get("levels"):
            lines += ["## 博主明确提及的绝对价位", ""]
            for item in view["levels"]:
                sources = item.get("sources") or []
                if sources:
                    seconds = sources[0]["start_seconds"]
                    lines.append(f"- {safe_text(item.get('asset'))} · {safe_text(item.get('role'))} "
                                 f"**{safe_text(item.get('value'))}** · [核对原视频 {stamp(seconds)}]({link(video_id, seconds)})"
                                 f"：「{safe_text(item.get('quote') or sources[0]['text'])}」")
            lines.append("")
        lines += ["## 图表画面与画线锚点", ""]
        charts = analysis.get("chart_evidence") or []
        if charts:
            lines += ["> [!warning] 字幕提到了图表，但下列时间点不等于图表画面已被核实。未取得原始图像时，画线起终点与对应价格均保持未知。", ""]
            for chart in charts:
                at = float(chart.get("start_seconds") or 0)
                lines += [f"- [查看原视频 {stamp(at)}]({link(video_id, at)}) · "
                          f"字幕提及：{safe_text(chart.get('topic'))}"]
                if chart.get("visual_check"):
                    lines.append(f"  - 原始画面核验：{safe_text(chart['visual_check'])}")
                else:
                    lines.append("  - 原始画面：未核验；画线起点、终点和对应价格未知。")
                for passage in chart.get("source_passages") or []:
                    second = float(passage["start_seconds"])
                    lines.append(f"  - [博主原话 {stamp(second)}]({link(video_id, second)})："
                                 f"{safe_text(passage['text'])[:400]}")
        else:
            lines.append("尚未取得可核对的图表画面信息；斐波那契比例不能单凭字幕换算成价格。")
        lines += ["", "## 发布后的市场走势", "",
                  "数据口径：表内交易所的 USDT 本位永续合约成交价，每 24 小时窗口内的完整小时 K 线。"
                  "窗口从视频发布时间起算；首尾不足整小时的部分不计入。不同交易所价格可能不同。"
                  "此处记录价格，不自动判定博主对错。", ""]
        if market_days:
            lines += ["| 交易所 | 交易对 | 发布后 | 开始 UTC | 开盘 | 最高 | 最低 | 收盘 |",
                      "|---|---|---:|---|---:|---:|---:|---:|"]
            for day in market_days:
                if day["day_number"] <= 7 or day["day_number"] % 7 == 0:
                    lines.append(f"| {day['venue']} | {day['symbol']} | 第 {day['day_number']} 天 | {day['window_start'][:16]} | "
                                 f"{day['open']} | {day['high']} | {day['low']} | {day['close']} |")
            hits = []
            for level in view.get("levels") or []:
                price = strict_price(str(level.get("value") or ""))
                symbol = symbol_for(str(level.get("asset") or ""))
                if price is None or symbol is None:
                    continue
                matching = [day for day in market_days if day["symbol"] == symbol and
                            Decimal(day["low"]) <= price <= Decimal(day["high"])]
                if matching:
                    hits.append(f"- {symbol} {safe_text(level['value'])}：第 {matching[0]['day_number']} 天的小时 K 线范围曾触及。")
            if hits:
                lines += ["", "### 价位触及记录", "",
                          "仅表示行情范围包含该绝对价位；不表示交易能成交，也不代表博主判断正确。"] + hits
        else:
            lines.append("首个完整的发布后 24 小时窗口尚未结束，或行情数据暂不可用。")
        lines += ["", "## 视频描述区原文", ""]
        description = str(row["description"] or "").strip()
        lines += source_quote(description) if description else ["尚未取得描述区内容。"]
        lines += ["", "## 资料局限", "", safe_text(analysis.get("limits")) or
                  "字幕和模型提取可能有误；图上锚点须核对原视频。", ""]
        return "\n".join(lines)
    lines += ["## 一眼看懂", "", safe_text(analysis.get("summary")), ""]
    lines += ["## 内容结构", ""]
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
        lines.append("内容记录中未提取出可靠的章节时间点。" if row["source_kind"] == "gemini_video"
                     else "字幕中未提取出可靠的章节时间点。")
    lines += ["", "## 开头钩子", "", safe_text(analysis.get("hook")), "",
              "## 关键观点与依据", ""]
    for claim in (analysis.get("claims") or [])[:15]:
        seconds = claim.get("start_seconds", 0)
        lines.append(f"- [{stamp(seconds)}]({link(video_id, seconds)}) **{safe_text(claim.get('claim'))}**：{safe_text(claim.get('evidence'))}")
    if not market_mode:
        lines += ["", "## 我的差异化选题机会", ""]
        for angle in (analysis.get("angles") or [])[:8]:
            lines.append(f"- **{safe_text(angle.get('idea'))}**：{safe_text(angle.get('why'))}")
    if include_topics:
        lines += ["", "## 主题与相关视频", ""]
        if topics:
            lines.append("主题：" + " · ".join(
                f"[[{topic_path(name).removesuffix('.md')}|{name}]]" for name in topics))
        else:
            lines.append("尚未完成主题分类。")
        for path, other_title, shared in (related or [])[:12]:
            lines.append(f"- [[{path.removesuffix('.md')}|{other_title}]] · 共同主题：{'、'.join(shared)}")
        if not related:
            lines.append("暂无已收录的同主题视频。")
    lines += ["", "## 视频描述区原文", ""]
    description = str(row["description"] or "").strip()
    lines += source_quote(description) if description else ["尚未取得描述区内容。"]
    if not market_mode:
        lines += ["", "## 观众评论 · 精选", ""]
        if comments is None:
            lines.append("尚未采集评论；需要启用 YouTube Data API v3。")
        elif not comments:
            lines.append("暂无可用的公开评论。")
        else:
            lines.append("评论反映个别观众体验，不代表全部观众；原文可能随后被作者修改或删除。")
            for item in comments[:10]:
                url = f"https://www.youtube.com/watch?v={video_id}&lc={quote(str(item['id']), safe='')}"
                category = safe_text(item.get("category"))
                lines += ["", f"### [{category} · {int(item.get('likes') or 0)} 赞]({url})", ""]
                lines += source_quote(str(item.get("text") or ""))
                if item.get("why"):
                    lines.append(f"筛选原因：{safe_text(item['why'])}")
        lines += ["", "## 播放数据快照", "", "| 采集时间 UTC | 播放量 |", "|---|---:|"]
        for snapshot in snapshots[-20:]:
            lines.append(f"| {snapshot['captured_at']} | {snapshot['view_count']:,} |")
        lines += ["", "> [!note] 热度判断", "> 播放量需与同频道、同视频类型、相同发布时长的数据比较。积累足够同期快照后再给出相对热度。", ""]
    lines += ["", "## 资料局限", "", safe_text(analysis.get("limits")) or "仅依据字幕，未核验画面内容。", ""]
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


def render_channel_index(name: str, url: str, videos: list[sqlite3.Row]) -> str:
    if url.startswith("@"):
        url = "https://www.youtube.com/" + url
    lines = [f"# {safe_text(name)}", "", f"[打开 YouTube 频道]({url})", "", "## 视频笔记", ""]
    if not videos:
        lines.append("尚无新视频笔记。频道加入前的视频默认不回溯分析。")
    for row in videos:
        title = safe_text(row["title"]).replace("|", "\\|")
        lines.append(f"- {published_local(row):%Y-%m-%d} · [[{note_path(row).removesuffix('.md')}|{title}]]")
    return "\n".join(lines) + "\n"


def render_market_channel_index(name: str, url: str, videos: list[sqlite3.Row]) -> str:
    lines = [f"# {safe_text(name)} · 市场观点时间线", "", f"[打开 YouTube 频道]({url})", "",
             "按发布时间排列模型归纳的博主观点；请点开原话核对。各期观点不自动判定相互延续、修正或正确。", ""]
    if not videos:
        lines.append("尚无已分析的视频。")
    for row in videos[:60]:
        title = safe_text(row["title"])
        lines += [f"## {published_local(row):%Y-%m-%d %H:%M} · [[{note_path(row).removesuffix('.md')}|{title}]]", ""]
        analysis = json.loads(row["analysis_json"] or "{}")
        view = analysis.get("market_view") or {}
        if row["source_kind"] == "gemini_video":
            lines.append("> [!warning] 本期依据 AI 近似内容记录，并非逐字字幕。")
        if view.get("version") != 2:
            lines.append("观点等待逐字证据回填。")
        elif view.get("positions"):
            for item in view["positions"][:5]:
                sources = item.get("sources") or []
                if sources:
                    second = sources[0]["start_seconds"]
                    lines.append(f"- **{safe_text(item.get('asset'))} · {safe_text(item.get('stance'))} · "
                                 f"{safe_text(item.get('horizon'))}**：{safe_text(item.get('view'))} "
                                 f"[原话]({link(row['id'], second)})")
        else:
            lines.append("未提取到明确方向判断；请查看该期记录的原话。")
        lines.append("")
    return "\n".join(lines)


def render_topic_index(groups: dict[str, list[sqlite3.Row]]) -> str:
    lines = ["# YouTube Radar · 主题索引", "", "按视频核心问题归类；同一视频可以属于多个主题。", ""]
    for name in sorted(groups):
        lines.append(f"- [[{topic_path(name).removesuffix('.md')}|{name}]] · {len(groups[name])} 条视频")
    return "\n".join(lines) + "\n"


def render_topic_note(name: str, videos: list[sqlite3.Row]) -> str:
    lines = ["---", f"title: {yaml_quote(name)}", "tags: [youtube-radar, youtube-radar/topic]",
             "---", "", f"# {name}", "", "## 相关视频", ""]
    for row in sorted(videos, key=lambda item: item["published_at"], reverse=True):
        lines.append(f"- {published_local(row):%Y-%m-%d} · {safe_text(row['channel_name'])} · "
                     f"[[{note_path(row).removesuffix('.md')}|{safe_text(row['title'])}]]")
    return "\n".join(lines) + "\n"

