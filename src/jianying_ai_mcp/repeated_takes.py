from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .asset_batch import _read_transcript, _spread
from .audio_production import _normalized
from .config import Settings


MAX_SOURCES = 96
MAX_WINDOWS = 960
MAX_CUES_PER_SOURCE = 320
MAX_CHARS = 220
MIN_CHARS = 16
MAX_NEAR_COMPARISONS = 5_000
NUMBERS = re.compile(r"[0-9]+|[零〇一二两三四五六七八九十百千万亿]+(?=人|次|份|件|期|天|年|月|分钟|秒|元|块|个|成)|[零〇一二两三四五六七八九十百千万亿]{2,}")
NEGATION = re.compile(r"不|没|无|非|拒绝|禁止|cannot|never|not")


@dataclass(frozen=True)
class _Take:
    source_id: str
    start_ms: int
    end_ms: int
    text: str
    normalized: str
    review_cues: int


def _take_windows(row: dict[str, Any], cues: list[dict[str, Any]]) -> tuple[list[_Take], bool]:
    usable = []
    for cue in cues:
        text, start, end = cue.get("text"), cue.get("start_ms"), cue.get("end_ms")
        if not isinstance(text, str) or type(start) is not int or type(end) is not int or not 0 <= start < end:
            continue
        usable.append((start, end, text, cue.get("quality") == "review"))
    usable.sort(key=lambda item: item[:2])
    trimmed = len(usable) > MAX_CUES_PER_SOURCE
    if trimmed:
        # Keep continuous groups of cues at the beginning, middle and end of long files.
        size = MAX_CUES_PER_SOURCE // 3
        middle = (len(usable) - size) // 2
        clipped_blocks = [usable[:size], usable[middle:middle + size], usable[-size:]]
    else:
        clipped_blocks = [usable]
    # Long ASR cues cannot be assigned a smaller exact time range from speech text alone.
    blocks = [[(a, b, text, _normalized(text) if len(text) <= 650 else "", review) for a, b, text, review in block]
              for block in clipped_blocks]

    def make(block: list[tuple[int, int, str, str, bool]]) -> _Take | None:
        if not block or not all(cue[3] for cue in block):
            return None
        text = " ".join(cue[2] for cue in block)
        normalized = "".join(cue[3] for cue in block)
        if not MIN_CHARS <= len(normalized) <= MAX_CHARS or len(set(normalized)) < 5:
            return None
        return _Take(row["source_id"], block[0][0], max(cue[1] for cue in block), text, normalized, sum(cue[4] for cue in block))

    words = {}
    if usable and not trimmed and usable[-1][1] - usable[0][0] <= 42_000:
        all_cues = make(blocks[0])
        if all_cues:
            words[(all_cues.start_ms, all_cues.end_ms)] = all_cues
    for block in blocks:
        for index, first in enumerate(block):
            close = []
            for cue in block[index:index + 3]:
                if (close and cue[0] - close[-1][1] > 1_250) or cue[1] - first[0] > 20_000:
                    break
                close.append(cue)
                item = make(close)
                if item:
                    words.setdefault((item.start_ms, item.end_ms), item)
    return list(words.values()), trimmed


def _overlaps(first: _Take, second: _Take) -> bool:
    return first.source_id == second.source_id and first.start_ms < second.end_ms and second.start_ms < first.end_ms


def _same_region(first: _Take, second: _Take) -> bool:
    if not _overlaps(first, second):
        return False
    common = min(first.end_ms, second.end_ms) - max(first.start_ms, second.start_ms)
    shortest = min(first.end_ms - first.start_ms, second.end_ms - second.start_ms)
    return common >= shortest * 0.4


def _similarity(first: _Take, second: _Take, threshold: float) -> float | None:
    a, b = first.normalized, second.normalized
    if a == b:
        return 1.0
    if 2 * min(len(a), len(b)) < threshold * (len(a) + len(b)) or NUMBERS.findall(a) != NUMBERS.findall(b):
        return None
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    if matcher.quick_ratio() < threshold:
        return None
    score = matcher.ratio()
    if score < threshold:
        return None
    for operation, left, right, start, end in matcher.get_opcodes():
        if operation == "equal":
            continue
        a_change, b_change = a[left:right], b[start:end]
        # A small change can reverse the statement or introduce a new qualification.
        if NEGATION.search(a_change + b_change) or max(len(a_change), len(b_change)) >= 9:
            return None
    return score


def _edges(takes: list[_Take], threshold: float) -> tuple[dict[int, dict[int, float]], bool]:
    exact: dict[str, list[int]] = defaultdict(list)
    index: dict[str, list[int]] = defaultdict(list)
    shingles = []
    for number, take in enumerate(takes):
        text = take.normalized
        grams = {text[i:i + 4] for i in range(len(text) - 3)}
        shingles.append(grams)
        for gram in grams:
            index[gram].append(number)
        exact[text].append(number)

    links: dict[int, dict[int, float]] = defaultdict(dict)
    near_comparisons = 0
    truncated = False
    for number, take in enumerate(takes):
        matches: Counter[int] = Counter()
        rarest = sorted(shingles[number], key=lambda gram: (len(index[gram]), gram))[:32]
        for gram in rarest:
            if len(index[gram]) <= 72:
                matches.update(other for other in index[gram] if other > number)
        remaining = [other for other in exact[take.normalized] if other > number]
        proposed = set(_spread(remaining, 16))
        proposed.update(other for other, shared in sorted(matches.items(), key=lambda pair: (-pair[1], pair[0]))[:24] if shared >= 2)
        for other in sorted(proposed):
            if _overlaps(take, takes[other]):
                continue
            if take.normalized != takes[other].normalized:
                if near_comparisons >= MAX_NEAR_COMPARISONS:
                    truncated = True
                    continue
                near_comparisons += 1
            score = _similarity(take, takes[other], threshold)
            if score is not None:
                links[number][other] = score
                links[other][number] = score
    return links, truncated


def _member(take: _Take, row: dict[str, Any], score: float) -> dict[str, Any]:
    speech = row.get("audio_evidence") or {}
    return {
        "source_id": take.source_id, "analysis_id": row.get("analysis_id"),
        "start_ms": take.start_ms, "end_ms": take.end_ms,
        "text": take.text[:112], "text_truncated": len(take.text) > 112,
        "similarity_to_first": round(score, 3), "transcript_source": row.get("transcript_source"),
        "needs_transcript_review": bool(take.review_cues),
        "source_speech_detected": bool(speech["speech_segments"]) if "speech_segments" in speech else None,
    }


def find_repeated_takes(
    settings: Settings, batch_id: str, *, source_ids: list[str] | None = None,
    max_groups: int = 8, max_members: int = 4, min_similarity: float = 0.9,
) -> dict[str, Any]:
    """Find similar transcript ranges in an existing local batch analysis."""
    if not isinstance(batch_id, str) or not re.fullmatch(r"[0-9a-f]{32}", batch_id):
        raise ValueError("batch_id 格式无效")
    if type(max_groups) is not int or not 1 <= max_groups <= 16 or type(max_members) is not int or not 2 <= max_members <= 6:
        raise ValueError("max_groups 必须是 1–16，max_members 必须是 2–6")
    if type(min_similarity) not in (int, float) or not 0.82 <= min_similarity <= 1:
        raise ValueError("min_similarity 必须在 0.82–1 之间")
    catalog = settings.work_root / "media_batches" / batch_id / "batch.json"
    rows = json.loads(catalog.read_text(encoding="utf-8"))["sources"]
    by_id = {row["source_id"]: row for row in rows}
    if source_ids is not None:
        if not isinstance(source_ids, list) or any(not isinstance(source, str) for source in source_ids):
            raise ValueError("source_ids 必须是素材编号数组")
        unknown = set(source_ids) - by_id.keys()
        if unknown:
            raise ValueError(f"批次中没有素材: {', '.join(sorted(unknown))}")
        requested = set(source_ids)
        rows = [row for row in rows if row["source_id"] in requested]
    available = [row for row in rows if row.get("transcript_count") and row.get("analysis_report_path")]
    selected = _spread(available, MAX_SOURCES)
    per_source = MAX_WINDOWS // max(1, len(selected))
    takes = []
    unavailable = []
    truncated = len(selected) < len(available)
    analysis_root = (settings.work_root / "video_analyses").resolve()
    for row in selected:
        try:
            report_path = Path(row["analysis_report_path"]).resolve(strict=True)
            if not report_path.is_relative_to(analysis_root) or report_path.stat().st_size > 8 * 1024 * 1024:
                unavailable.append(row["source_id"])
                continue
            windows, clipped = _take_windows(row, _read_transcript(row))
        except (OSError, json.JSONDecodeError):
            unavailable.append(row["source_id"])
            continue
        # Preserve the whole-take candidate where it fits, then spread across the rest.
        kept = windows[:1] + _spread(windows[1:], per_source - 1)
        takes.extend(kept)
        truncated |= clipped or len(kept) < len(windows)

    links, pairs_truncated = _edges(takes, min_similarity)
    truncated |= pairs_truncated
    pairs = [(first, other, score) for first, neighbors in links.items() for other, score in neighbors.items() if first < other]
    pairs.sort(key=lambda pair: (-min(len(takes[pair[0]].normalized), len(takes[pair[1]].normalized)) * pair[2], -pair[2], pair[:2]))
    groups = []
    used = []
    for first, other, _ in pairs:
        if any(_same_region(takes[index], prior) for index in (first, other) for prior in used):
            continue
        if len(groups) >= max_groups:
            truncated = True
            break
        members = [first, other]
        common = links[first].keys() & links[other].keys()
        for index in sorted(common, key=lambda candidate: (-min(links[first][candidate], links[other][candidate]), candidate)):
            if len(members) >= max_members:
                break
            if any(member not in links[index] or _overlaps(takes[index], takes[member]) for member in members):
                continue
            if any(_same_region(takes[index], prior) for prior in used):
                continue
            members.append(index)
        members.sort()
        first = members[0]
        scores = [links[a][b] for a in members for b in members if a < b]
        groups.append({
            "minimum_similarity": round(min(scores), 3),
            "exact_transcript_match": all(score == 1 for score in scores),
            "takes": [_member(takes[i], by_id[takes[i].source_id], 1 if i == first else links[first][i]) for i in members],
        })
        used.extend(takes[i] for i in members)

    return {
        "batch_id": batch_id, "groups": groups, "transcript_sources_available": len(available),
        "transcript_sources_scanned": len(selected) - len(unavailable), "windows_compared": len(takes),
        "unavailable_source_ids": unavailable, "truncated": truncated,
        "basis": "缓存对白文字相似度。请回看候选片段确认不同用词、表达和画面，再决定保留哪一条。",
    }
