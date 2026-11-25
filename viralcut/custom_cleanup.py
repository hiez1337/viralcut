"""Storage accounting and safe disk cleanup helpers for ViralCut."""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from . import db, jobs
from .config import DATA_DIR, MODELS_DIR, PROJECTS_DIR

_CACHE_LOCK = threading.Lock()
_CLEAN_LOCK = threading.Lock()
_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_CACHE_TTL = 5.0

_ACTIVE_STATUSES = ("queued", "preparing", "running", "processing")
_SOURCE_ELIGIBLE_STATUSES = ("done", "cancelled", "error")
_TEMP_PART_SUFFIXES = (".part", ".ytdl", ".tmp")


def invalidate_cache() -> None:
    """Drop the cached storage summary so the next read re-scans disk."""
    with _CACHE_LOCK:
        _CACHE["ts"] = 0.0
        _CACHE["data"] = None


def _dir_size(path: Path) -> int:
    try:
        st = path.stat()
    except OSError:
        return 0
    if not os.path.isdir(path):
        return int(st.st_size)
    total = 0
    stack = [str(path)]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += int(entry.stat(follow_symlinks=False).st_size)
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _active_project_ids(*, strict: bool = False) -> Set[str]:
    active: Set[str] = set(jobs.active_project_ids())
    try:
        with db.session() as conn:
            rows = conn.execute(
                "SELECT id FROM projects WHERE status IN ('queued','preparing','running','processing')"
            ).fetchall()
            for row in rows:
                active.add(str(row["id"]))
            clip_rows = conn.execute(
                "SELECT DISTINCT c.project_id FROM clips c "
                "JOIN projects p ON p.id = c.project_id "
                "WHERE c.status='rendering' AND p.status NOT IN ('cancelled', 'error')"
            ).fetchall()
            for row in clip_rows:
                active.add(str(row["project_id"]))
    except Exception:
        if strict:
            raise
    return active


def _all_projects_map(*, strict: bool = False) -> Dict[str, Dict[str, Any]]:
    projects: Dict[str, Dict[str, Any]] = {}
    try:
        with db.session() as conn:
            rows = conn.execute("SELECT id, status, source, meta FROM projects").fetchall()
            for row in rows:
                raw_meta = row["meta"]
                try:
                    meta = json.loads(raw_meta or "{}") if raw_meta else {}
                except Exception:
                    meta = {}
                projects[str(row["id"])] = {
                    "id": str(row["id"]),
                    "status": str(row["status"] or ""),
                    "source": row["source"],
                    "meta": meta if isinstance(meta, dict) else {},
                }
    except Exception:
        if strict:
            raise
    return projects


def _add_unique(
    targets: List[Tuple[str, Path]],
    seen: Set[str],
    pid: str,
    path: Path,
) -> int:
    try:
        if not path.exists():
            return 0
    except OSError:
        return 0
    key = str(path.resolve())
    if key in seen:
        return 0
    seen.add(key)
    sz = _dir_size(path)
    targets.append((pid, path))
    return sz


def _collect_targets(*, strict: bool = False, include_models: bool = True) -> Tuple[
    Dict[str, Any],
    List[Tuple[str, Path]],
    List[Tuple[str, Path]],
    Dict[str, Optional[str]],
]:
    active_ids = _active_project_ids(strict=strict)
    projects_map = _all_projects_map(strict=strict)

    temp_targets: List[Tuple[str, Path]] = []
    source_targets: List[Tuple[str, Path]] = []
    source_pids: Dict[str, Optional[str]] = {}
    audio_cleared_pids: Dict[str, Optional[str]] = {}
    seen_temp: Set[str] = set()
    seen_source: Set[str] = set()

    clips_bytes = 0
    temp_bytes = 0
    sources_bytes = 0
    projects_bytes = 0
    active_skipped = 0

    if PROJECTS_DIR.is_dir():
        try:
            entries = sorted(PROJECTS_DIR.iterdir(), key=lambda p: p.name)
        except OSError:
            entries = []
        for pdir in entries:
            if not pdir.is_dir():
                projects_bytes += _dir_size(pdir)
                continue
            projects_bytes += _dir_size(pdir)
            pid = pdir.name
            for sub in ("clips", "previews", "thumbs", "ass"):
                clips_bytes += _dir_size(pdir / sub)

            if pid in active_ids:
                active_skipped += 1
                continue

            proj = projects_map.get(pid)
            if proj is None:
                # Orphan project folder no longer tracked in DB.
                temp_bytes += _add_unique(temp_targets, seen_temp, pid, pdir)
                continue

            status = proj.get("status") or ""
            meta = proj.get("meta") or {}

            # Safe temporary artifacts for any non-active project.
            temp_bytes += _add_unique(temp_targets, seen_temp, pid, pdir / "audio16k.wav")
            temp_bytes += _add_unique(temp_targets, seen_temp, pid, pdir / "sections")
            temp_bytes += _add_unique(temp_targets, seen_temp, pid, pdir / "preview_src")

            for pattern in (
                "whisper_chunks_*",
                "win_*.wav",
                "src_*.mp4",
                "recheck_*.wav",
                "viralcut_*.zip",
                "*.part",
                "*.ytdl",
                "*.tmp",
            ):
                for match in pdir.glob(pattern):
                    temp_bytes += _add_unique(temp_targets, seen_temp, pid, match)

            seg_dir = pdir / "segments"
            seg_m3u8_bytes = 0
            if seg_dir.is_dir():
                for playlist in seg_dir.glob("*.m3u8"):
                    added = _add_unique(temp_targets, seen_temp, pid, playlist)
                    temp_bytes += added
                    seg_m3u8_bytes += added

            if status == "done":
                temp_bytes += _add_unique(temp_targets, seen_temp, pid, pdir / "preview.mp4")
                if meta.get("segments_mode"):
                    for audio_file in pdir.glob("audio.*"):
                        if audio_file.suffix.lower() not in _TEMP_PART_SUFFIXES:
                            if _add_unique(temp_targets, seen_temp, pid, audio_file) > 0:
                                temp_bytes += audio_file.stat().st_size
                                audio_cleared_pids[pid] = proj.get("source")

            if status in _SOURCE_ELIGIBLE_STATUSES:
                proj_source_added = 0
                for src_file in pdir.glob("source.*"):
                    if src_file.suffix.lower() in _TEMP_PART_SUFFIXES:
                        continue
                    proj_source_added += _add_unique(source_targets, seen_source, pid, src_file)
                if seg_dir.is_dir():
                    seg_sz = _add_unique(source_targets, seen_source, pid, seg_dir)
                    if seg_sz > 0:
                        proj_source_added += max(0, seg_sz - seg_m3u8_bytes)
                sources_bytes += proj_source_added
                if proj_source_added > 0:
                    source_pids[pid] = proj.get("source")

    models_bytes = _dir_size(MODELS_DIR) if include_models else 0

    stats = {
        "projects_mb": round(projects_bytes / 1048576.0, 1),
        "clips_mb": round(clips_bytes / 1048576.0, 1),
        "temp_mb": round(temp_bytes / 1048576.0, 1),
        "temp_items": len(temp_targets),
        "sources_mb": round(sources_bytes / 1048576.0, 1),
        "sources_items": len(source_targets),
        "models_mb": round(models_bytes / 1048576.0, 1),
        "active_skipped": active_skipped,
        "data_dir": str(DATA_DIR),
        "_audio_cleared_pids": audio_cleared_pids,
    }
    return stats, temp_targets, source_targets, source_pids


def storage_summary(force: bool = False) -> Dict[str, Any]:
    """Return cached disk usage breakdown and cleanable byte totals."""
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get("data")
        if not force and cached is not None and (now - float(_CACHE.get("ts") or 0.0)) < _CACHE_TTL:
            return dict(cached)

    stats, _, _, _ = _collect_targets(strict=False, include_models=True)
    stats.pop("_audio_cleared_pids", None)
    with _CACHE_LOCK:
        _CACHE["ts"] = time.monotonic()
        _CACHE["data"] = stats
    return dict(stats)


def run_cleanup(scope: str = "temp") -> Dict[str, Any]:
    """Safely delete temporary files ('temp') or finished/cancelled sources ('sources')."""
    if scope not in ("temp", "sources"):
        raise ValueError("Неизвестный режим очистки: выберите 'temp' или 'sources'.")

    with _CLEAN_LOCK:
        pre_stats, temp_targets, source_targets, source_pids = _collect_targets(
            strict=True, include_models=False,
        )
        audio_cleared_pids = pre_stats.pop("_audio_cleared_pids", {})
        targets = temp_targets if scope == "temp" else source_targets
        active_ids = _active_project_ids(strict=True)

        freed_bytes = 0
        removed = 0
        skipped = 0

        for pid, path in targets:
            if pid in active_ids:
                skipped += 1
                continue
            try:
                if not path.exists():
                    continue
                before = _dir_size(path)
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
                after = _dir_size(path) if path.exists() else 0
                freed_bytes += max(0, before - after)
                if not path.exists():
                    removed += 1
                else:
                    skipped += 1
            except OSError:
                skipped += 1

        clear_map = source_pids if scope == "sources" else audio_cleared_pids
        for pid, src_raw in clear_map.items():
            if pid in active_ids:
                continue
            if src_raw:
                try:
                    if not Path(str(src_raw)).exists():
                        db.update_project(pid, source=None)
                except OSError:
                    db.update_project(pid, source=None)

        invalidate_cache()
        summary = storage_summary(force=True)
        return {
            "ok": True,
            "scope": scope,
            "freed_mb": round(freed_bytes / 1048576.0, 1),
            "removed": removed,
            "skipped": skipped,
            "storage": summary,
        }
