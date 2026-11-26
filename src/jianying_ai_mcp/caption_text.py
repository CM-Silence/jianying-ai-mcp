"""Caption line breaking and punctuation for Chinese short-video subtitles.

Two rules keep burned-in captions readable:

* Break at clause boundaries. A caption ends where the speaker pauses, so a
  cue never starts with the comma that belonged to the previous clause and a
  word is not cut in half just because a character budget ran out.
* Short-video captions do not print sentence punctuation. Commas, full stops
  and enumeration marks become a space (or disappear at the end of a cue);
  question and exclamation marks, quotes and title marks stay because they
  carry tone or meaning.
"""

from __future__ import annotations

import math
import re
from typing import Any, Literal

from .production_graphics import ProductionError


CaptionPunctuation = Literal["spaced", "keep", "strip"]
PUNCTUATION_MODES: tuple[str, ...] = ("spaced", "keep", "strip")

# A clause ends after one of these; the mark stays attached to the clause it closes.
_CLAUSE_END = "，。！？；：、,.!?;:…—"
# Marks that must never open a caption line.
_NO_LEADING = "，。！？；：、,.!?;:…）)」』”’》〉】]—·"
# Pause marks that become a space inside a caption and vanish at its edges.
_PAUSE = "，。；：、,.;:…—"
# Tone marks survive "spaced" mode.
_TONE = "！？!?"
_CLAUSE_RE = re.compile(rf"[^{re.escape(_CLAUSE_END)}]+[{re.escape(_CLAUSE_END)}]*|[{re.escape(_CLAUSE_END)}]+")


def visible_length(text: str) -> int:
    """Characters a viewer reads; punctuation and spaces do not count."""
    return sum(1 for char in text if not char.isspace() and char not in _NO_LEADING and char not in _PAUSE)


# Without a word segmenter, function words are the safest places to cut a long
# clause: break *before* these ...
_CUT_BEFORE = set("就也都才还而但却又和跟与及并把被让给对向从在是要会能可如果因为所以")
# ... or *after* these structural particles.
_CUT_AFTER = set("的了着过得地")


def _clauses(text: str) -> list[str]:
    parts: list[str] = []
    for match in _CLAUSE_RE.finditer(text):
        chunk = match.group(0)
        if not chunk.strip():
            continue
        if parts and (chunk.strip()[0] in _NO_LEADING or (parts[-1].endswith("、") and visible_length(parts[-1]) <= 4)):
            # Stray punctuation runs fold back, and a short enumeration ("做细、做准、做透")
            # stays one unit so it is never split across two captions.
            parts[-1] += chunk.strip()
        else:
            parts.append(chunk.strip())
    return parts


def _cut_points(body: str) -> list[int]:
    points = []
    for index in range(2, len(body) - 1):
        if body[index] in _CUT_BEFORE or body[index - 1] in _CUT_AFTER:
            points.append(index)
    return points


def _split_long(clause: str, max_chars: int, min_chars: int) -> list[str]:
    """Cut an over-long clause into balanced pieces at function-word boundaries."""
    body = clause.rstrip(_CLAUSE_END)
    tail = clause[len(body):]
    count = max(1, math.ceil(visible_length(body) / max_chars))
    if count == 1:
        return [clause]
    candidates = _cut_points(body)
    pieces, begin = [], 0
    for part in range(1, count):
        ideal = round(len(body) * part / count)
        usable = [p for p in candidates if begin + min_chars <= p <= begin + max_chars and len(body) - p >= min_chars]
        cut = min(usable, key=lambda p: abs(p - ideal)) if usable else min(ideal, begin + max_chars)
        pieces.append(body[begin:cut])
        begin = cut
    pieces.append(body[begin:])
    if len(pieces) > 1 and visible_length(pieces[-1]) < min_chars:
        pieces[-2] += pieces.pop()
    pieces[-1] += tail
    return pieces


def _pack(units: list[str], max_chars: int) -> list[str]:
    """Group clause units into the fewest lines, then the most even ones."""
    sizes = [visible_length(unit) for unit in units]
    count = len(units)
    best: list[tuple[int, int, int] | None] = [None] * (count + 1)
    best[0] = (0, 0, 0)
    for end in range(1, count + 1):
        length = 0
        for begin in range(end - 1, -1, -1):
            length += sizes[begin]
            if length > max_chars and begin != end - 1:
                break
            previous = best[begin]
            if previous is None:
                continue
            candidate = (previous[0] + 1, previous[1] + (max_chars - min(length, max_chars)) ** 2, begin)
            if best[end] is None or candidate[:2] < best[end][:2]:
                best[end] = candidate
    lines, end = [], count
    while end:
        begin = best[end][2]
        lines.append("".join(units[begin:end]))
        end = begin
    return lines[::-1]


def split_caption(text: str, *, max_chars: int = 14, min_chars: int = 4) -> list[str]:
    """Split spoken text into caption cues at clause boundaries.

    Sentences (and clauses closed by ；：) always end a cue. Inside a sentence,
    clauses are grouped into the fewest cues of at most ``max_chars`` visible
    characters, as evenly as possible. A clause longer than that is cut at a
    function-word boundary. Punctuation stays with the clause it closes, so
    no cue starts with a mark.
    """
    if type(max_chars) is not int or not 6 <= max_chars <= 40:
        raise ProductionError("字幕每条字数上限需为 6–40")
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    cues: list[str] = []
    for line in lines:
        sentence: list[str] = []
        for clause in _clauses(line):
            pieces = _split_long(clause, max_chars, min_chars) if visible_length(clause) > max_chars else [clause]
            sentence.extend(pieces)
            if clause[-1] in "。！？!?…；;：:":
                cues.extend(_pack(sentence, max_chars))
                sentence = []
        if sentence:
            cues.extend(_pack(sentence, max_chars))
    # Never leave a tiny orphan behind when it can join its neighbour.
    merged: list[str] = []
    for cue in cues:
        if merged and visible_length(cue) < min_chars and visible_length(merged[-1]) + visible_length(cue) <= max_chars + 2:
            merged[-1] += cue
        else:
            merged.append(cue)
    return merged


def tidy_caption(text: str, punctuation: CaptionPunctuation = "spaced") -> str:
    """Apply the caption punctuation policy to one cue."""
    if punctuation not in PUNCTUATION_MODES:
        raise ProductionError(f"caption_punctuation 支持 {'、'.join(PUNCTUATION_MODES)}")
    lines = []
    for line in str(text or "").splitlines():
        line = line.strip().lstrip(_NO_LEADING + " 　")
        if punctuation == "keep":
            lines.append(line)
            continue
        removable = _PAUSE + (_TONE if punctuation == "strip" else "")
        # Decimal points, thousands separators and times belong to the spoken number.
        numeric = ".,:"
        pauses = "".join(ch for ch in removable if ch not in numeric)
        line = re.sub(rf"[{re.escape(pauses)}]+|(?<!\d)[.,:]|[.,:](?!\d)", " ", line)
        line = re.sub(r"[ 　]{2,}", " ", line).strip()
        # A tone mark hugs its clause even after a following pause became a space.
        line = re.sub(rf"\s+([{re.escape(_TONE)}])", r"\1", line)
        if line:
            lines.append(line)
    return "\n".join(line for line in lines if line)


def caption_cues(
    text: str, start: float, end: float, *, max_chars: int = 14,
    punctuation: CaptionPunctuation = "spaced",
) -> list[tuple[float, float, str]]:
    """Split ``text`` and spread the pieces over ``start``–``end`` by reading length."""
    pieces = [(piece, tidy_caption(piece, punctuation)) for piece in split_caption(text, max_chars=max_chars)]
    pieces = [(raw, shown) for raw, shown in pieces if shown]
    if not pieces or end <= start:
        return []
    weights = [max(1, visible_length(raw)) for raw, _ in pieces]
    total = sum(weights)
    span = end - start
    cursor = start
    cues = []
    for (_, shown), weight in zip(pieces, weights):
        stop = cursor + span * weight / total
        cues.append((cursor, stop, shown))
        cursor = stop
    return cues


# ---------------------------------------------------------------- presentation

CAPTION_MODES = ("single", "stack", "blocks", "words")



def _group(phrases: list[str], count: int) -> list[str]:
    """Merge adjacent phrases into ``count`` groups of similar length."""
    if count <= 1 or len(phrases) <= count:
        return phrases
    groups: list[str] = []
    index = 0
    while index < len(phrases):
        left_groups = count - len(groups)
        left_phrases = len(phrases) - index
        take = max(1, round(left_phrases / left_groups))
        groups.append(" ".join(phrases[index:index + take]))
        index += take
    return groups


def _read_chunks(line: str, count: int) -> list[str]:
    """Cut one caption line into ``count`` readable chunks, never mid-word.

    Captions keep a space where a pause mark was, so those spaces are the safe
    places to break. Only a line with no internal pause falls back to a
    function-word cut.
    """
    phrases = [part for part in line.split(" ") if part]
    if len(phrases) > 1:
        return _group(phrases, count)
    body = line.rstrip(_CLAUSE_END)
    if visible_length(body) <= count * 2:
        return [line]
    cuts = _cut_points(body)
    parts, begin = [], 0
    for index in range(1, count):
        ideal = round(len(body) * index / count)
        usable = [p for p in cuts if begin + 3 <= p <= len(body) - 3 and p > begin]
        cut = min(usable, key=lambda p: abs(p - ideal)) if usable else ideal
        parts.append(body[begin:cut])
        begin = cut
    parts.append(body[begin:] + line[len(body):])
    return [part.strip() for part in parts if part.strip()]


_BREAK = re.compile(r"(?:[。！？!?；;：\n]|[，、]|(?<!\d)[.,:]|[.,:](?!\d))+")


def follows_caption_step(previous: str, current: str) -> bool:
    """Explicit consecutive list items can arrive in separate timed ASR cues."""
    ordinal = r"^第?([一二三四五六七八九十]|\d+)(?=[、,，.．：:）)\s]|点|项|步|是)"
    left, right = re.match(ordinal, previous), re.match(ordinal, current)
    if left and right:
        def index(mark: str) -> int:
            return int(mark) if mark.isdecimal() else "一二三四五六七八九十".index(mark) + 1
        return index(right[1]) == index(left[1]) + 1
    return bool(re.match(r"^(?:首先|先(?!生|天|前)|其次|然后|接着|再(?!见))", previous)
                and re.match(r"^(?:其次|然后|接着|再(?!见)|最后)", current))


def semantic_caption_runs(text: str, max_chars: int) -> list[list[str]]:
    """Group only authored clauses or explicit list/step markers, before stripping punctuation."""
    runs: list[list[str]] = []
    sentence: list[str] = []

    def flush() -> None:
        if not sentence:
            return
        lengths = [visible_length(part) for part in sentence]
        parts = [part.strip().rstrip(_CLAUSE_END) for part in sentence]
        fillers = {"其实", "但是", "如果", "因为", "所以", "比如", "也就是说", "换句话说"}
        incomplete = ("不能", "不得", "不超过", "不低于", "不高于", "至少", "包括", "以及", "对于", "的", "和", "与")
        enumeration = any("、" in p for p in sentence)
        progression = all(follows_caption_step(a, b) for a, b in zip(sentence, sentence[1:]))
        # A time/place lead-in followed by a comma still belongs to the same statement.
        framing = re.fullmatch(r"(?:今天|昨天|明天|当天|本周|上周|这天)(?:上午|下午|早上|晚上|中午)?|"
                               r"(?:在|从).{1,12}(?:之前|之后|的时候|期间|来看|来说)", parts[0])
        clauses = not framing and min(lengths) >= 4 and sum(lengths) >= 12
        eligible = (len(parts) >= 2 and max(lengths) <= 2 * max_chars and min(lengths) >= 1
                    and not any(p in fillers or p.endswith(incomplete) for p in parts[:-1])
                    and (enumeration or progression or clauses))
        if eligible:
            # A fresh page starts after at most three fixed slots; avoid an isolated fourth item.
            pos = 0
            while pos < len(sentence):
                count = 2 if len(sentence) - pos == 4 else 3
                runs.append(sentence[pos:pos + count])
                pos += count
        else:
            runs.append(["".join(sentence)])
        sentence.clear()

    start = 0
    for mark in _BREAK.finditer(text):
        # Keep punctuation following a short enumeration marker with its item.
        if re.fullmatch(r"\s*(?:第?[一二三四五六七八九十]+|\d+)", text[start:mark.start()]) and mark[0] in ("、", ".", "，"):
            continue
        part = text[start:mark.end()].strip()
        if part:
            sentence.append(part)
        start = mark.end()
        if any(ch in "。！？!?；;：:.\n" for ch in mark[0]):
            # Consecutive explicit steps can span sentence punctuation.
            if not (sentence and follows_caption_step(sentence[-1], text[start:].lstrip())):
                flush()
    if tail := text[start:].strip():
        sentence.append(tail)
    flush()
    return runs


def caption_entities(
    cues: list[tuple[float, float, str]], *, mode: str = "single", groups: list[int | None] | None = None,
) -> list[dict[str, Any]]:
    """A multi-caption group owns its slots from the first reveal until the group ends."""
    if mode not in CAPTION_MODES:
        raise ProductionError(f"caption_mode 支持 {'、'.join(CAPTION_MODES)}")
    groups = groups if groups is not None else [None] * len(cues)
    if len(groups) != len(cues):
        raise ProductionError("字幕分组与时码数目不符")
    entities = []
    pos = 0
    while pos < len(cues):
        start, end, text = cues[pos]
        stop = pos + 1
        if mode in ("stack", "blocks") and groups[pos] is not None:
            while stop < len(cues) and groups[stop] == groups[pos]:
                stop += 1
        if stop - pos > 1:
            siblings = [c[2] for c in cues[pos:stop]]
            for slot, (at, cue_end, phrase) in enumerate(cues[pos:stop]):
                entities.append({"start": at, "end": cues[stop - 1][1], "text": phrase, "slot": slot,
                                 "siblings": siblings, "cue_end": cue_end, "anim": mode})
        elif mode == "words":
            chunks = _read_chunks(text, 3)
            span = (end - start) / len(chunks)
            for i in range(len(chunks)):
                entities.append({"start": start + i * span, "end": start + (i + 1) * span,
                                 "text": " ".join(chunks[:i + 1]), "anim": "words"})
        else:
            entities.append({"start": start, "end": end, "text": text, "anim": "single"})
        pos = stop
    return entities


def emphasis_ranges(text: str, terms: list[str] | None) -> list[tuple[int, int]]:
    """Character ranges of the emphasised words present in one caption box."""
    ranges: list[tuple[int, int]] = []
    for term in terms or []:
        if not term:
            continue
        ranges.extend((match.start(), match.end()) for match in re.finditer(re.escape(term), text))
    return ranges
