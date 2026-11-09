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


def _md_cell(value: Any, max_len: int = 90) -> str:
    text = safe_text(value).replace("|", "\\|")
    return (text[: max_len - 1] + "…") if len(text) > max_len else (text or "—")


def _infer_stance_from_text(text: str) -> str:
    t = safe_text(text)
    if any(w in t for w in ("看空", "做空", "反弹空", "下跌", "破位", "暴跌", "熊市", "跌破", "回调", "回撤")):
        if any(w in t for w in ("看多", "做多", "抄底", "暴涨", "牛回", "反转")):
            return "多空条件推演"
        return "偏空/看跌"
    if any(w in t for w in ("看多", "做多", "低多", "看涨", "上涨", "突破", "筑底", "抄底")):
        return "偏多/看涨"
    return "震荡/观察"


def _evaluate_market_outcome(asset: str, stance: str, levels: list[dict[str, Any]],
                             market_days: list[sqlite3.Row] | None) -> tuple[str, str]:
    if not market_days:
        return "⏳ 窗口未满24h", "⏳ 待行情验证"
    sym = symbol_for(asset) or "BTCUSDT"
    bars = [d for d in market_days if d["symbol"] == sym and d["day_number"] <= 3]
    if not bars:
        bars = [d for d in market_days if d["day_number"] <= 3]
    if not bars:
        return "⏳ 暂无对应K线", "⏳ 待行情验证"
    try:
        open_p = Decimal(str(bars[0]["open"]))
        high_p = max(Decimal(str(d["high"])) for d in bars)
        low_p = min(Decimal(str(d["low"])) for d in bars)
        close_p = Decimal(str(bars[-1]["close"]))
    except Exception:
        return "—", "—"
    if open_p <= 0:
        return "—", "—"
    pct_close = float((close_p - open_p) / open_p * 100)
    pct_high = float((high_p - open_p) / open_p * 100)
    pct_low = float((low_p - open_p) / open_p * 100)
    days_label = f"{len(bars)}d"
    price_str = f"开 {open_p:,.0f} → 高 {high_p:,.0f}({pct_high:+.1f}%) / 低 {low_p:,.0f}({pct_low:+.1f}%) / 收 {close_p:,.0f}({pct_close:+.1f}%)"

    hit_vals = []
    for lv in levels:
        if symbol_for(str(lv.get("asset") or asset)) == sym:
            p = strict_price(str(lv.get("value") or ""))
            if p is not None and low_p <= p <= high_p:
                hit_vals.append(str(lv.get("value")))
    hit_suffix = f" · 🎯触及 {','.join(hit_vals[:2])}" if hit_vals else ""

    s = safe_text(stance)
    if any(k in s for k in ("空", "跌")):
        if pct_low <= -1.8 or pct_close <= -1.2:
            verdict = f"✅ 与看空一致 ({days_label}最大跌 {pct_low:.1f}%){hit_suffix}"
        elif pct_close >= 2.0 and pct_low > -1.2:
            verdict = f"❌ 走势相反 ({days_label}收涨 {pct_close:+.1f}%){hit_suffix}"
        else:
            verdict = f"➖ 震荡微调 ({days_label}收 {pct_close:+.1f}%){hit_suffix}"
    elif any(k in s for k in ("多", "涨")):
        if pct_high >= 1.8 or pct_close >= 1.2:
            verdict = f"✅ 与看多一致 ({days_label}最大涨 {pct_high:+.1f}%){hit_suffix}"
        elif pct_close <= -2.0 and pct_high < 1.2:
            verdict = f"❌ 走势相反 ({days_label}收跌 {pct_close:+.1f}%){hit_suffix}"
        else:
            verdict = f"➖ 震荡微调 ({days_label}收 {pct_close:+.1f}%){hit_suffix}"
    else:
        verdict = f"📊 区间波动 (高{pct_high:+.1f}% / 低{pct_low:+.1f}%){hit_suffix}"
    return price_str, verdict


def _collect_summary_rows(row: sqlite3.Row, analysis: dict[str, Any]) -> list[dict[str, Any]]:
    view = analysis.get("market_view") or {}
    positions = view.get("positions") or []
    levels = view.get("levels") or []
    charts = analysis.get("chart_evidence") or []
    visual_levels = [lv for c in charts for lv in (c.get("visual_levels") or []) if isinstance(lv, dict)]
    all_levels = levels + visual_levels

    def zones_for(asset: str) -> tuple[str, str]:
        sup, res, other = [], [], []
        for lv in all_levels:
            a = safe_text(lv.get("asset"))
            if a and asset and a.upper() != asset.upper() and a not in asset and asset not in a:
                continue
            role = safe_text(lv.get("role"))
            val = safe_text(lv.get("value"))
            if not val:
                continue
            tag = f"{val}(图)" if lv in visual_levels else val
            if any(k in role for k in ("支撑", "多", "底", "买", "0.618", "0.66")):
                if tag not in sup:
                    sup.append(tag)
            elif any(k in role for k in ("阻力", "压力", "空", "顶", "前高")):
                if tag not in res:
                    res.append(tag)
            else:
                if tag not in other:
                    other.append(tag)
        sup_str = " / ".join(sup[:3]) or (" / ".join(other[:2]) if other else "—")
        res_str = " / ".join(res[:3]) or "—"
        return sup_str, res_str

    summary_rows = []
    for item in positions:
        asset = safe_text(item.get("asset") or "BTC")
        sup_auto, res_auto = zones_for(asset)
        sup = safe_text(item.get("support_zone")) or sup_auto
        res = safe_text(item.get("resistance_zone")) or res_auto
        sources = item.get("sources") or []
        sec = sources[0]["start_seconds"] if sources else 0
        plan = safe_text(item.get("view"))
        if item.get("condition"):
            plan += f"（前提：{safe_text(item['condition'])}）"
        if item.get("invalidation"):
            plan += f"（失效：{safe_text(item['invalidation'])}）"
        summary_rows.append({
            "asset": asset,
            "horizon": safe_text(item.get("horizon") or "未明确"),
            "stance": safe_text(item.get("stance") or _infer_stance_from_text(plan)),
            "support": sup,
            "resistance": res,
            "plan": plan,
            "seconds": sec,
        })
    if not summary_rows:
        stmts = view.get("statements") or []
        by_asset: dict[str, list[dict[str, Any]]] = {}
        for s in stmts:
            by_asset.setdefault(safe_text(s.get("asset") or "BTC"), []).append(s)
        for asset, items in by_asset.items():
            combined = "；".join(safe_text(x.get("quote")) for x in items[:2])
            sources = items[0].get("sources") or []
            sec = sources[0]["start_seconds"] if sources else 0
            sup, res = zones_for(asset)
            summary_rows.append({
                "asset": asset,
                "horizon": "综合观察",
                "stance": _infer_stance_from_text(combined or safe_text(row["title"])),
                "support": sup,
                "resistance": res,
                "plan": combined or safe_text(row["title"]),
                "seconds": sec,
            })
    if not summary_rows:
        sup, res = zones_for("BTC")
        summary_rows.append({
            "asset": "BTC",
            "horizon": "未明确",
            "stance": _infer_stance_from_text(safe_text(row["title"])),
            "support": sup,
            "resistance": res,
            "plan": f"（根据标题与画面归纳）{safe_text(row['title'])}",
            "seconds": 0,
        })
    return summary_rows


def render_note(row: sqlite3.Row, analysis: dict[str, Any], language: str,
                snapshots: list[sqlite3.Row], topics: list[str] | None = None,
                related: list[tuple[str, str, list[str]]] | None = None,
                comments: list[dict[str, Any]] | None = None,
                include_topics: bool = True, market_mode: bool = False,
                market_days: list[sqlite3.Row] | None = None,
                prior_videos: list[tuple[sqlite3.Row, list[sqlite3.Row]]] | None = None) -> str:
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
        summary_rows = _collect_summary_rows(row, analysis)
        overview = safe_text(view.get("overview")) or "；".join(
            f"{r['asset']}（{r['stance']}）：{r['plan'][:60]}" for r in summary_rows[:3]
        )
        lines += [
            "## 一眼看懂 · 核心态度与点位总表",
            "",
            f"> [!important] 核心结论：{overview}",
            "",
            "| 标的 | 周期 | 博主态度 | 关键支撑 / 做多区 | 关键阻力 / 做空区 | 核心推演剧本 (Plan A / Plan B) | 原片核对 |",
            "|---|---|---|---|---|---|---|",
        ]
        for sr in summary_rows[:8]:
            lines.append(
                f"| **{_md_cell(sr['asset'], 16)}** | {_md_cell(sr['horizon'], 18)} | "
                f"**{_md_cell(sr['stance'], 18)}** | {_md_cell(sr['support'], 32)} | "
                f"{_md_cell(sr['resistance'], 32)} | {_md_cell(sr['plan'], 110)} | "
                f"[{stamp(sr['seconds'])}]({link(video_id, sr['seconds'])}) |"
            )
        lines.append("")

        if prior_videos:
            lines += [
                "## 与前序视频观点对比（连续性追踪）",
                "",
                "| 期次 | 发布日期 | 视频标题 | 当时态度 (主标的) | 当时关键点位与推演 | 随后真实行情表现 | 推演验证 |",
                "|---|---|---|---|---|---|---|",
            ]
            cur_btc = next((r for r in summary_rows if "BTC" in r["asset"].upper()), summary_rows[0])
            for idx, (p_row, p_days) in enumerate(prior_videos[:3], start=1):
                p_analysis = json.loads(p_row["analysis_json"] or "{}")
                p_view = p_analysis.get("market_view") or {}
                p_sr = _collect_summary_rows(p_row, p_analysis)
                p_main = next((r for r in p_sr if "BTC" in r["asset"].upper()), p_sr[0])
                p_levels = (p_view.get("levels") or []) + [
                    lv for c in (p_analysis.get("chart_evidence") or []) for lv in (c.get("visual_levels") or [])
                ]
                price_str, verdict = _evaluate_market_outcome(p_main["asset"], p_main["stance"], p_levels, p_days)
                p_title = _md_cell(p_row["title"], 28)
                p_link = f"[[{note_path(p_row).removesuffix('.md')}\\|{p_title}]]" if p_row["artifact_base"] else p_title
                zones = f"支:{p_main['support']} / 阻:{p_main['resistance']}" if (p_main["support"] != "—" or p_main["resistance"] != "—") else ""
                plan_cell = _md_cell((f"[{zones}] " if zones else "") + p_main["plan"], 75)
                lines.append(
                    f"| 前 {idx} 期 | {published_local(p_row):%m-%d %H:%M} | {p_link} | "
                    f"**{p_main['asset']} · {p_main['stance']}** | {plan_cell} | "
                    f"{_md_cell(price_str, 65)} | {verdict} |"
                )
            prev_main = _collect_summary_rows(prior_videos[0][0], json.loads(prior_videos[0][0]["analysis_json"] or "{}"))[0]
            same_dir = (
                ("空" in cur_btc["stance"] and "空" in prev_main["stance"])
                or ("多" in cur_btc["stance"] and "多" in prev_main["stance"])
                or (cur_btc["stance"] == prev_main["stance"])
            )
            change_tag = "🔄 **观点延续**" if same_dir else "⚠️ **态度修正/转向**"
            lines += [
                "",
                f"- {change_tag}：上期（{published_local(prior_videos[0][0]):%m-%d}）对 **{prev_main['asset']}** 态度为「**{prev_main['stance']}**」，"
                f"本期（{published_local(row):%m-%d}）态度为「**{cur_btc['stance']}**」（支撑关注：{cur_btc['support']}，阻力关注：{cur_btc['resistance']}）。",
                "",
            ]

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
                lines += ["本期没有提取到可逐字核对的明确方向判断（请参考顶部汇总表与下方观察原话）。", ""]
            for item in (view.get("statements") or [])[:8]:
                sources = item.get("sources") or []
                if sources:
                    seconds = sources[0]["start_seconds"]
                    lines.append(f"- 盘面观察 · {safe_text(item.get('asset'))}："
                                 f"[原视频 {stamp(seconds)}]({link(video_id, seconds)})「{safe_text(item.get('quote'))}」")
            if view.get("statements"):
                lines.append("")
        charts = analysis.get("chart_evidence") or []
        visual_levels = [
            (c.get("start_seconds") or 0, lv)
            for c in charts
            for lv in (c.get("visual_levels") or [])
            if isinstance(lv, dict) and lv.get("value")
        ]
        if view.get("levels") or visual_levels:
            lines += ["## 博主明确提及的绝对价位（含图表视觉识别）", ""]
            for item in view.get("levels") or []:
                sources = item.get("sources") or []
                if sources:
                    seconds = sources[0]["start_seconds"]
                    lines.append(f"- 🗣️ 语音提及 · {safe_text(item.get('asset'))} · {safe_text(item.get('role'))} "
                                 f"**{safe_text(item.get('value'))}** · [核对原视频 {stamp(seconds)}]({link(video_id, seconds)})"
                                 f"：「{safe_text(item.get('quote') or sources[0]['text'])}」")
            for sec, lv in visual_levels:
                lines.append(f"- 👁️ 图表画面识别 · {safe_text(lv.get('asset'))} · {safe_text(lv.get('role'))} "
                             f"**{safe_text(lv.get('value'))}** · [查看图表画面 {stamp(sec)}]({link(video_id, sec)})"
                             f"（{safe_text(lv.get('note'))}）")
            lines.append("")
        lines += ["## 图表画面与画线锚点", ""]
        if charts:
            for chart in charts:
                at = float(chart.get("start_seconds") or 0)
                lines += [f"- [查看原视频 {stamp(at)}]({link(video_id, at)}) · "
                          f"字幕提及：{safe_text(chart.get('topic'))}"]
                if chart.get("visual_check"):
                    lines.append(f"  - 👁️ **Gemini 图表视觉读盘**：{safe_text(chart['visual_check'])}")
                    for lv in chart.get("visual_levels") or []:
                        if isinstance(lv, dict) and lv.get("value"):
                            lines.append(f"    - 图上价位：{safe_text(lv.get('asset'))} {safe_text(lv.get('role'))} "
                                         f"**{safe_text(lv.get('value'))}**（{safe_text(lv.get('note'))}）")
                else:
                    lines.append("  - 原始画面：待视觉模型核验或画面未显示清晰价格标签。")
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
            main_sr = next((r for r in summary_rows if "BTC" in r["asset"].upper()), summary_rows[0])
            all_lvs = (view.get("levels") or []) + [lv for _, lv in visual_levels]
            p_str, v_str = _evaluate_market_outcome(main_sr["asset"], main_sr["stance"], all_lvs, market_days)
            lines += [
                f"- **走势与观点对照（{main_sr['asset']} · 博主态度：{main_sr['stance']}）**：{v_str}",
                f"- **发布后 72h 内行情区间**：{p_str}",
                "",
                "| 交易所 | 交易对 | 发布后 | 开始 UTC | 开盘 | 最高 | 最低 | 收盘 |",
                "|---|---|---:|---|---:|---:|---:|---:|",
            ]
            for day in market_days:
                if day["day_number"] <= 7 or day["day_number"] % 7 == 0:
                    lines.append(f"| {day['venue']} | {day['symbol']} | 第 {day['day_number']} 天 | {day['window_start'][:16]} | "
                                 f"{day['open']} | {day['high']} | {day['low']} | {day['close']} |")
            hits = []
            for level in all_lvs:
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


def render_market_channel_index(name: str, url: str, videos: list[sqlite3.Row],
                                days_by_video: dict[str, list[sqlite3.Row]] | None = None) -> str:
    days_by_video = days_by_video or {}
    lines = [f"# {safe_text(name)} · 市场观点时间线", "", f"[打开 YouTube 频道]({url})", "",
             "按发布时间排列模型归纳的博主观点；请点开原话核对。下方总表将每期观点、关键点位与发布后 72 小时真实行情对照。", ""]
    if not videos:
        lines.append("尚无已分析的视频。")
        return "\n".join(lines)

    lines += [
        "## 历史推演与行情验证追溯表",
        "",
        "| 发布时间 | 视频笔记 | 标的 | 核心态度 | 支撑/做多 | 阻力/做空 | 推演剧本摘要 | 发布后行情 (开→高/低→收) | 走势对照 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in videos[:60]:
        analysis = json.loads(row["analysis_json"] or "{}")
        view = analysis.get("market_view") or {}
        summary_rows = _collect_summary_rows(row, analysis)
        v_days = days_by_video.get(row["id"]) or []
        all_lvs = (view.get("levels") or []) + [
            lv for c in (analysis.get("chart_evidence") or []) for lv in (c.get("visual_levels") or [])
        ]
        t_cell = _md_cell(row["title"], 26)
        n_link = f"[[{note_path(row).removesuffix('.md')}\\|{t_cell}]]" if row["artifact_base"] else t_cell
        for idx, sr in enumerate(summary_rows[:2]):
            p_str, v_str = _evaluate_market_outcome(sr["asset"], sr["stance"], all_lvs, v_days)
            date_col = f"{published_local(row):%Y-%m-%d %H:%M}" if idx == 0 else "↳ 同期次标的"
            link_col = n_link if idx == 0 else "—"
            lines.append(
                f"| {date_col} | {link_col} | **{_md_cell(sr['asset'], 12)}** | "
                f"**{_md_cell(sr['stance'], 14)}** | {_md_cell(sr['support'], 20)} | "
                f"{_md_cell(sr['resistance'], 20)} | {_md_cell(sr['plan'], 65)} | "
                f"{_md_cell(p_str, 55)} | {v_str} |"
            )
    lines += ["", "## 逐期观点明细", ""]
    for row in videos[:60]:
        title = safe_text(row["title"])
        lines += [f"### {published_local(row):%Y-%m-%d %H:%M} · [[{note_path(row).removesuffix('.md')}|{title}]]", ""]
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
            for sr in _collect_summary_rows(row, analysis)[:2]:
                lines.append(f"- **{sr['asset']} · {sr['stance']}**：{sr['plan']}")
        lines.append("")
    return "\n".join(lines)


def render_market_panorama(channels: list[sqlite3.Row],
                           videos_by_channel: dict[int, list[sqlite3.Row]],
                           days_by_video: dict[str, list[sqlite3.Row]],
                           now: datetime) -> str:
    local_now = now.astimezone(timezone(timedelta(hours=8)))
    lines = [
        "# 加密与美股博主 · 多空全景看板",
        "",
        f"> **最后更新时间（北京时间）：** {local_now:%Y-%m-%d %H:%M}  ",
        "> 汇总所有已跟踪市场博主的最新一期态度、关键支撑/阻力位及近期推演与真实行情对照，方便快速掌握市场多空共识与分歧。",
        "",
        "## 一、 各博主最新一期核心态度总览",
        "",
        "| 博主频道 | 最新视频时间 | 最新视频笔记 | 主标的 | 最新态度 | 关键支撑 / 做多区 | 关键阻力 / 做空区 | 核心推演剧本摘要 | 与上期对比 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    bull_list, bear_list, neutral_list = [], [], []
    all_recent: list[sqlite3.Row] = []

    for ch in channels:
        vids = videos_by_channel.get(ch["id"]) or []
        all_recent.extend(vids[:8])
        if not vids:
            lines.append(f"| **{safe_text(ch['name'] or ch['url'])}** | — | 尚无已分析视频 | — | — | — | — | — | — |")
            continue
        latest = vids[0]
        analysis = json.loads(latest["analysis_json"] or "{}")
        sr_list = _collect_summary_rows(latest, analysis)
        main_sr = next((r for r in sr_list if "BTC" in r["asset"].upper()), sr_list[0])
        if len(vids) >= 2:
            prev_sr = _collect_summary_rows(vids[1], json.loads(vids[1]["analysis_json"] or "{}"))[0]
            same = (
                ("空" in main_sr["stance"] and "空" in prev_sr["stance"])
                or ("多" in main_sr["stance"] and "多" in prev_sr["stance"])
                or (main_sr["stance"] == prev_sr["stance"])
            )
            delta_str = f"🔄 延续 ({prev_sr['stance']})" if same else f"⚠️ 转向 (上期:{prev_sr['stance']})"
        else:
            delta_str = "🆕 首次记录"

        t_cell = _md_cell(latest["title"], 24)
        n_link = f"[[{note_path(latest).removesuffix('.md')}\\|{t_cell}]]" if latest["artifact_base"] else t_cell
        ch_name = safe_text(ch["name"] or ch["url"])
        lines.append(
            f"| **{ch_name}** | {published_local(latest):%m-%d %H:%M} | {n_link} | "
            f"**{main_sr['asset']}** | **{main_sr['stance']}** | {_md_cell(main_sr['support'], 24)} | "
            f"{_md_cell(main_sr['resistance'], 24)} | {_md_cell(main_sr['plan'], 75)} | {delta_str} |"
        )
        tag = f"**{ch_name}**（{main_sr['asset']}: {main_sr['stance']}，支:{main_sr['support']} / 阻:{main_sr['resistance']}）"
        if any(k in main_sr["stance"] for k in ("空", "跌")):
            bear_list.append(tag)
        elif any(k in main_sr["stance"] for k in ("多", "涨")):
            bull_list.append(tag)
        else:
            neutral_list.append(tag)

    lines += [
        "",
        "## 二、 当前博主多空阵营分布（按最新一期）",
        "",
        f"- 🟢 **偏多 / 看涨阵营（{len(bull_list)} 人）**：{'；'.join(bull_list) if bull_list else '暂无'}",
        f"- 🔴 **偏空 / 看跌阵营（{len(bear_list)} 人）**：{'；'.join(bear_list) if bear_list else '暂无'}",
        f"- 🟡 **震荡 / 条件推演（{len(neutral_list)} 人）**：{'；'.join(neutral_list) if neutral_list else '暂无'}",
        "",
        "## 三、 近期跨博主推演 vs 真实行情验证追溯表（最近 25 期）",
        "",
        "| 发布日期 | 博主 | 视频笔记 | 标的 | 当时态度 | 关键支撑/阻力 | 当时推演摘要 | 发布后 72h 行情 (开→高/低→收) | 市场走势对照 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    all_recent.sort(key=lambda r: r["published_at"], reverse=True)
    for row in all_recent[:25]:
        analysis = json.loads(row["analysis_json"] or "{}")
        view = analysis.get("market_view") or {}
        sr_list = _collect_summary_rows(row, analysis)
        main_sr = next((r for r in sr_list if "BTC" in r["asset"].upper()), sr_list[0])
        v_days = days_by_video.get(row["id"]) or []
        all_lvs = (view.get("levels") or []) + [
            lv for c in (analysis.get("chart_evidence") or []) for lv in (c.get("visual_levels") or [])
        ]
        p_str, v_str = _evaluate_market_outcome(main_sr["asset"], main_sr["stance"], all_lvs, v_days)
        t_cell = _md_cell(row["title"], 22)
        n_link = f"[[{note_path(row).removesuffix('.md')}\\|{t_cell}]]" if row["artifact_base"] else t_cell
        zones = f"支:{main_sr['support']} / 阻:{main_sr['resistance']}"
        lines.append(
            f"| {published_local(row):%m-%d %H:%M} | **{safe_text(row['channel_name'])}** | {n_link} | "
            f"**{main_sr['asset']}** | **{main_sr['stance']}** | {_md_cell(zones, 28)} | "
            f"{_md_cell(main_sr['plan'], 60)} | {_md_cell(p_str, 52)} | {v_str} |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


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

