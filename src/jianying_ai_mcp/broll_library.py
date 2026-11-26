"""Search local video intervals by the visual observations attached to them."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Literal

from .config import Settings
from .jianying_stock import is_draft_media, stock_origin, stock_roots
from .media_info import MediaProbeError, parse_media_info
from .native_stock import read_stock_selection
from .production_graphics import absolute_file


@contextmanager
def _library(settings: Settings):
    path = settings.work_root / "broll_library" / "shots.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=15)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("""CREATE TABLE IF NOT EXISTS shots(
            id TEXT PRIMARY KEY, payload TEXT NOT NULL, duration_ms INTEGER NOT NULL,
            file_size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS terms(
            term TEXT NOT NULL, shot_id TEXT NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
            weight INTEGER NOT NULL, PRIMARY KEY(term, shot_id)) WITHOUT ROWID""")
        db.execute("CREATE INDEX IF NOT EXISTS terms_by_shot ON terms(shot_id)")
        db.execute("""CREATE TABLE IF NOT EXISTS native_stock(
            shot_id TEXT PRIMARY KEY, receipt TEXT NOT NULL)""")
        yield db


_HAN = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f"
_WORDS = re.compile(rf"[{_HAN}]+|[^\W_{_HAN}]+")


def _terms(text: str, *, indexing: bool = False) -> set[str]:
    """Han bigrams work without whitespace. Also index characters for one-character queries."""
    terms = set()
    for word in _WORDS.findall(unicodedata.normalize("NFKC", text).casefold()):
        if not re.fullmatch(rf"[{_HAN}]+", word):
            terms.add(word)
            continue
        if indexing or len(word) == 1:
            terms.update(word)
        terms.update(word[i:i + 2] for i in range(len(word) - 1))
    return terms


def _stamp(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _analysis_shots(settings: Settings, analysis_id: str) -> list[dict]:
    if not re.fullmatch(r"[0-9a-f]{20}", analysis_id):
        raise ValueError("analysis_id 格式无效")
    folder = settings.work_root / "video_analyses" / analysis_id
    report_file = folder / "analysis.json"
    report = json.loads(report_file.read_text(encoding="utf-8"))
    annotations = json.loads((folder / "observations.json").read_text(encoding="utf-8"))
    path = absolute_file(report["local_video_path"], "分析素材")
    size, mtime = _stamp(path)
    identity = report.get("source_identity")
    # Older report formats did not retain the fingerprint used for the analysis cache.
    if identity:
        changed = (size, mtime) != (identity["size"], identity["mtime_ns"])
    else:
        changed = mtime > report_file.stat().st_mtime_ns
    if changed:
        raise ValueError("分析后的素材已变动，请重新观察并标注源区间")

    result = []
    for candidate in annotations.get("edit_candidates", []):
        # save_video_observations also accepts numeric strings in legacy reports.
        start, end = int(candidate["start_ms"]), int(candidate["end_ms"])
        observations = [o for o in annotations.get("observations", []) if start <= int(o["time_ms"]) < end]
        description = candidate.get("description") or "；".join(dict.fromkeys(o["description"] for o in observations))
        if not description:
            raise ValueError(f"候选 {start}–{end} ms 需要画面描述或区间内的 observation")
        tags = []
        for item in [candidate, *observations]:
            values = item.get("tags", [])
            if not isinstance(values, list):
                raise ValueError("观察标签 tags 必须是文字数组")
            tags.extend(values)
        shot = {"source": str(path), "start_ms": start, "end_ms": end, "description": description,
                "tags": tags, "analysis_id": analysis_id, "use_case": candidate.get("reason") or "",
                "_analysis_stamp": (size, mtime)}
        if report.get("downloaded"):
            shot["source_url"] = report["source"]
        for field in ("source_url", "license", "attribution"):
            if candidate.get(field):
                shot[field] = candidate[field]
        frames = [f for f in report.get("frames", []) if start <= f["time_ms"] < end and Path(f["path"]).is_file()]
        if frames:
            closest = min(frames, key=lambda f: abs((start + end) / 2 - f["time_ms"]))
            shot["preview_frame"] = {"time_ms": closest["time_ms"], "path": closest["path"]}
        result.append(shot)
    if not result:
        raise ValueError("请先用 save_video_observations 保存带 start_ms 和 end_ms 的 edit_candidates")
    return result


SourcePool = Literal["jianying", "personal"]


def index_broll_shots(
    settings: Settings, shots: list[dict] | None = None, *, analysis_id: str | None = None,
    source_pool: SourcePool = "jianying",
) -> dict[str, Any]:
    """Upsert a whole batch atomically, reusing IDs for revisions to the same source interval."""
    if analysis_id is not None:
        if shots is not None:
            raise ValueError("请在 shots 与 analysis_id 中选择一种输入")
        shots = _analysis_shots(settings, analysis_id)
    if not isinstance(shots, list) or not shots:
        raise ValueError("请提供至少一个带画面描述和源区间的镜头")
    if source_pool not in ("jianying", "personal"):
        raise ValueError("source_pool 需为 jianying 或 personal")
    roots = stock_roots(settings) if source_pool == "jianying" else {}
    sources = {}
    prepared = {}
    for shot in shots:
        if not isinstance(shot, dict):
            raise ValueError("每个镜头须为对象")
        path = absolute_file(shot.get("source"), "B-roll 素材")
        if is_draft_media(settings, path):
            raise ValueError("草稿内的素材只作参考。请使用独立保存的剪映素材库下载文件")
        origin = stock_origin(roots, path)
        if source_pool == "jianying" and not origin:
            raise ValueError("剪映素材需来自共享在线素材缓存或 JIANYING_STOCK_ROOT 指定的剪映下载目录")
        if path not in sources:
            stamp = _stamp(path)
            try:
                info = parse_media_info(settings, str(path))["media_info"]
            except MediaProbeError:
                verified = shot.get("editor_verified_ms")
                if (origin != "online_material_cache" or path.suffix.casefold() != ".mp4"
                        or not settings.ffprobe_exe or type(verified) is not int or verified <= 0):
                    raise
                sources[path] = stamp, verified, "editor_only"
            else:
                seconds = info.get("video_duration") or info["duration"]
                if info["type"] != "video" or not seconds:
                    raise ValueError(f"B-roll 镜头需要有视频流和时长的素材: {path}")
                sources[path] = stamp, round(seconds * 1000), "local_video"
        stamp, duration_ms, availability = sources[path]
        if analysis_id is not None and shot["_analysis_stamp"] != stamp:
            raise ValueError(f"已分析的素材发生变化: {path}")
        start, end = shot.get("start_ms"), shot.get("end_ms")
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= duration_ms:
            raise ValueError(f"源区间须为 0–{duration_ms} 毫秒内的整数且 end_ms 大于 start_ms: {path}")
        description, tags = shot.get("description"), shot.get("tags", [])
        if not isinstance(description, str) or not description.strip():
            raise ValueError("每条镜头都需要非空的画面描述 description")
        if not isinstance(tags, list) or not all(isinstance(tag, str) and tag.strip() for tag in tags):
            raise ValueError("tags 必须是非空文字组成的数组")
        identity = [os.path.normcase(str(path)), *stamp, start, end]
        if source_pool == "jianying":
            identity.insert(0, source_pool)
        key = json.dumps(identity, ensure_ascii=False)
        shot_id = hashlib.sha256(key.encode()).hexdigest()[:32]
        entry = {"shot_id": shot_id, "source": str(path), "start_ms": start, "end_ms": end,
                 "description": description.strip(), "tags": list(dict.fromkeys(tag.strip() for tag in tags))}
        entry["source_pool"] = source_pool
        entry["availability"] = availability
        if availability == "editor_only":
            entry["editor_verified_ms"] = duration_ms
        if origin:
            entry["origin"] = origin
        for field in ("use_case", "source_url", "license", "attribution", "asset_title", "asset_id"):
            value = shot.get(field)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{field} 需要文字")
            if value and value.strip():
                entry[field] = value.strip()
        if analysis_id is not None:
            entry["analysis_id"] = analysis_id
            if "preview_frame" in shot:
                entry["preview_frame"] = shot["preview_frame"]
        prepared[shot_id] = entry, stamp

    with _library(settings) as db:
        for path, (stamp, _, _) in sources.items():
            if _stamp(path) != stamp:
                raise ValueError(f"素材在建索引期间发生变化: {path}")
        for shot_id, (entry, stamp) in prepared.items():
            if entry.get("asset_id"):
                bound = db.execute("SELECT json_extract(receipt, '$.asset_id') FROM native_stock WHERE shot_id=?",
                                   (shot_id,)).fetchone()
                if bound and bound[0] != entry["asset_id"]:
                    raise ValueError("该镜头已绑定其他剪映资源 ID，请核对素材")
            payload = json.dumps(entry, ensure_ascii=False)
            db.execute("INSERT OR REPLACE INTO shots VALUES (?, ?, ?, ?, ?)",
                       (shot_id, payload, entry["end_ms"] - entry["start_ms"], *stamp))
            weights = dict.fromkeys(_terms(entry["description"], indexing=True), 1)
            weights.update(dict.fromkeys(_terms(entry.get("asset_title", ""), indexing=True), 2))
            weights.update(dict.fromkeys(_terms(entry.get("use_case", ""), indexing=True), 2))
            weights.update(dict.fromkeys(_terms(" ".join(entry["tags"]), indexing=True), 3))
            db.executemany("INSERT INTO terms VALUES (?, ?, ?)",
                           ((term, shot_id, weight) for term, weight in weights.items()))
    return {"success": True, "indexed_count": len(prepared), "shot_ids": list(prepared)}


def bind_stock_selection(settings: Settings, selection: str, shot_ids: list[str]) -> dict:
    if not isinstance(shot_ids, list) or not shot_ids or not all(isinstance(k, str) and k for k in shot_ids):
        raise ValueError("请提供要绑定的已入库官方镜头 shot_ids")
    entries = read_stock_selection(settings, selection)
    bound = {}
    with _library(settings) as db:
        for key in dict.fromkeys(shot_ids):
            row = db.execute("SELECT s.*, n.receipt FROM shots s LEFT JOIN native_stock n ON n.shot_id=s.id WHERE s.id=?",
                             (key,)).fetchone()
            if row is None:
                raise ValueError(f"未找到镜头 {key}")
            shot = _live_shot(settings, row)
            receipt = entries.get(Path(shot["source"]))
            if not receipt or shot["source_pool"] != "jianying" or shot.get("origin") != "online_material_cache":
                raise ValueError(f"官方采集稿与镜头 {key} 的缓存源不匹配")
            if shot["end_ms"] * 1000 > receipt["video"]["duration"] or (
                shot.get("asset_id") and shot["asset_id"] != receipt["asset_id"]
            ):
                raise ValueError(f"镜头 {key} 的区间或资源 ID 与采集稿不一致")
            db.execute("INSERT OR REPLACE INTO native_stock VALUES (?,?)",
                       (key, json.dumps(receipt, ensure_ascii=False)))
            bound[key] = receipt["asset_id"]
    return {"bound_count": len(bound), "asset_ids": bound}


def _live_shot(settings: Settings, row: sqlite3.Row, *, with_receipt: bool = False) -> dict:
    shot = json.loads(row["payload"])
    shot.setdefault("source_pool", "personal")
    shot.setdefault("availability", "local_video")
    if is_draft_media(settings, Path(shot["source"]).resolve()):
        raise ValueError("该旧索引来自草稿，请从剪映素材库重选")
    if _stamp(Path(shot["source"])) != (row["file_size"], row["mtime_ns"]):
        raise ValueError(f"B-roll 素材已变动，请重新入库: {shot['source']}")
    preview = shot.get("preview_frame")
    if preview and not Path(preview["path"]).is_file():
        del shot["preview_frame"]
    receipt = row["receipt"] if "receipt" in row.keys() else None
    if receipt:
        data = json.loads(receipt)
        shot.update(availability="native_draft", asset_id=data["asset_id"],
                    width=data["video"]["width"], height=data["video"]["height"], duration_us=data["video"]["duration"])
        if with_receipt:
            shot["receipt"] = data
    return shot


def resolve_broll_shot(settings: Settings, shot_id: str, *, with_receipt: bool = False) -> dict:
    with _library(settings) as db:
        row = db.execute("SELECT s.*, n.receipt FROM shots s LEFT JOIN native_stock n ON n.shot_id=s.id WHERE s.id=?",
                         (shot_id,)).fetchone()
        if row is None:
            raise ValueError(f"找不到 B-roll 镜头 {shot_id}")
        return _live_shot(settings, row, with_receipt=with_receipt)


Availability = Literal["any", "local_video", "editor_only", "native_draft", "timeline_ready"]


def get_broll_shot(settings: Settings, shot_id: str) -> dict:
    """Resolve a catalog hit to the actual media handle supported by this machine."""
    shot = resolve_broll_shot(settings, shot_id)
    source = shot.pop("source")
    kind = shot["availability"]
    if kind == "local_video":
        media = {"kind": kind, "path": source}
    elif kind == "native_draft":
        media = {"kind": kind, "asset_id": shot["asset_id"], "shot_id": shot_id}
    else:
        media = None
    return {**shot, "media": media, "timeline_ready": media is not None,
            "source_range_ms": {"start": shot["start_ms"], "end": shot["end_ms"]},
            "next_action": "place_broll_shots" if media else "bind_jianying_stock_selection"}


def search_broll_shots(
    settings: Settings, query: str, *, min_duration_ms: int = 0, limit: int = 10,
    source_pool: SourcePool = "jianying",
    availability: Availability = "any",
) -> dict[str, Any]:
    if availability not in ("any", "local_video", "editor_only", "native_draft", "timeline_ready"):
        raise ValueError("未知的素材可用状态 availability")
    if source_pool not in ("jianying", "personal"):
        raise ValueError("source_pool 需为 jianying 或 personal")
    if type(min_duration_ms) is not int or min_duration_ms < 0:
        raise ValueError("min_duration_ms 需为非负整数")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit 需为 1–100 的整数")
    if not isinstance(query, str) or len(query) > 500 or not (terms := _terms(query)):
        raise ValueError("query 需要 1–500 字的口播、画面描述或标签")
    placeholders = ",".join("?" for _ in terms)
    sql = f"""WITH ranked AS (
              SELECT s.*, n.receipt, SUM(t.weight) AS score,
                     CASE WHEN n.shot_id IS NOT NULL THEN 'native_draft'
                          ELSE COALESCE(json_extract(s.payload, '$.availability'), 'local_video') END AS availability
              FROM terms t JOIN shots s ON s.id = t.shot_id
              LEFT JOIN native_stock n ON n.shot_id=s.id
              WHERE t.term IN ({placeholders}) AND s.duration_ms >= ?
              AND COALESCE(json_extract(s.payload, '$.source_pool'), 'personal') = ?
              GROUP BY s.id)
              SELECT * FROM ranked WHERE ?='any' OR availability=?
              OR (?='timeline_ready' AND availability IN ('native_draft','local_video'))
              ORDER BY score DESC, id"""
    matches = []
    skipped = 0
    with _library(settings) as db:
        for row in db.execute(sql, (*terms, min_duration_ms, source_pool, availability, availability, availability)):
            try:
                shot = _live_shot(settings, row)
            except (OSError, ValueError):
                skipped += 1
                continue
            matches.append({**shot, "score": row["score"]})
            if len(matches) == limit:
                break
    return {"success": True, "query": query, "source_pool": source_pool,
            "matches": matches, "unavailable_skipped": skipped}
