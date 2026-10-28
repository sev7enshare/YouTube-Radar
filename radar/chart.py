from __future__ import annotations

import re
from typing import Any


CHART_WORDS = re.compile(r"0[.,](?:5|618|66|786)|斐波那契|费波|fibonacci|fib\b|趋势线|趨勢線|畫線|画线",
                         re.IGNORECASE)


def chart_candidates(segments: list[dict[str, Any]], max_count: int = 2) -> list[dict[str, Any]]:
    """Locate spoken chart references; the visual model must verify the picture."""
    found: list[dict[str, Any]] = []
    for segment in segments:
        text = str(segment.get("text") or "")
        if not CHART_WORDS.search(text):
            continue
        at = int(float(segment["start"]))
        if any(abs(at - item["at"]) < 90 for item in found):
            continue
        found.append({"at": at, "topic": text[:80]})
        if len(found) >= max_count:
            break
    return found
