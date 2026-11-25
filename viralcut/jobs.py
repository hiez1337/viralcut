"""Фоновые задачи: очередь проектов и перерендер клипов."""
from __future__ import annotations

import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict

from . import db, live, pipeline, resources
from .config import MAX_WORKERS

_project_pool = ThreadPoolExecutor(max_workers=max(1, MAX_WORKERS), thread_name_prefix="project")
# Размер пула фиксирован (его нельзя менять на ходу), а фактическое число
# параллельных рендеров ограничивает шлагбаум по «мощности» из настроек.
_clip_pool = ThreadPoolExecutor(max_workers=max(1, resources.max_render_workers()),
                                thread_name_prefix="clip")
_clip_gate = resources.ConcurrencyGate(resources.render_workers)
_lock = threading.RLock()
_clip_running: set[str] = set()
_clip_running_projects: Dict[str, str] = {}
_agent_edits: list[Dict[str, Any]] = []
_AGENT_HISTORY_LIMIT = 50

# Каждый эфир — отдельный поток. Раньше стримы шли через пул на 3 места, и
# несколько «ждущих эфир» записей навсегда занимали очередь: следующая запись
# висела на «Запись · подключение», хотя ничего не начиналось.
MAX_STREAMS = max(1, int(os.environ.get("VIRALCUT_MAX_STREAMS", "6")))
_streams: Dict[str, threading.Thread] = {}
_streams_lock = threading.Lock()


_running: Dict[str, int] = {}
_pending: Dict[str, Any] = {}
_running_lock = threading.Lock()


def is_running(pid: str) -> bool:
    """Идёт ли уже обработка этого проекта (защита от двойного запуска)."""
    with _running_lock:
        return bool(_running.get(pid))


def active_project_ids() -> list[str]:
    """Вернуть список ID проектов, обрабатываемых прямо сейчас."""
    ids: list[str] = []
    with _running_lock:
        ids.extend(_running.keys())
    with _streams_lock:
        for pid, thread in _streams.items():
            if thread.is_alive() and pid not in ids:
                ids.append(pid)
    with _lock:
        for pid in _clip_running_projects.values():
            if pid and pid not in ids:
                ids.append(pid)
    return ids


def _mark(pid: str, delta: int) -> None:
    with _running_lock:
        count = _running.get(pid, 0) + delta
        if count <= 0:
            _running.pop(pid, None)
        else:
            _running[pid] = count


def cancel_pending(pid: str) -> None:
    """Убрать отложенную задачу проекта (например, при удалении)."""
    with _running_lock:
        _pending.pop(pid, None)


def _guarded(pid: str, target, *, reset_cancel: bool = False) -> None:
    """Запускает задачу, если для проекта ещё нет активной обработки.

    Если обработка уже идёт (например, пользователь нажал «повторить» во время
    отмены), задача ставится в очередь на этот же проект и стартует сразу после
    завершения текущей — вместо второй параллельной обработки.
    """
    with _running_lock:
        if _running.get(pid):
            _pending[pid] = (target, reset_cancel)
            return
        _running[pid] = 1

    def work() -> None:
        try:
            if reset_cancel:
                pipeline.clear_cancel(pid)
            target()
        except Exception as exc:  # noqa: BLE001
            db.update_project(pid, status="error", error=str(exc)[:600], stage="Ошибка")
        finally:
            with _running_lock:
                _running.pop(pid, None)
                nxt = _pending.pop(pid, None)
            if nxt is not None:
                nxt_target, nxt_reset = nxt
                _guarded(pid, nxt_target, reset_cancel=nxt_reset)

    _project_pool.submit(work)


def submit_project(pid: str) -> None:
    _guarded(pid, lambda: pipeline.run_project(pid))


def submit_retry(pid: str, *, prepare_only: bool = False, segments_hd: bool = False) -> None:
    def _run() -> None:
        if segments_hd:
            db.reset_clips_pending(pid)
            pipeline.process_project(pid)
            return
        db.clear_clips(pid)
        if prepare_only:
            pipeline.prepare_project(pid)
        else:
            pipeline.run_project(pid)

    _guarded(pid, _run, reset_cancel=True)


def submit_stream(pid: str, options: dict) -> None:
    with _streams_lock:
        existing = _streams.get(pid)
        if existing is not None and existing.is_alive():
            return
        if sum(1 for t in _streams.values() if t.is_alive()) >= MAX_STREAMS:
            db.update_project(
                pid, status="error", stage="Слишком много одновременных записей",
                error=f"Одновременно можно записывать не больше {MAX_STREAMS} эфиров.")
            return

        def work() -> None:
            try:
                live.run_stream(pid, options)
            except Exception as exc:  # noqa: BLE001
                db.update_project(pid, status="error", error=str(exc)[:600], stage="Ошибка стрима")
            finally:
                with _streams_lock:
                    _streams.pop(pid, None)

        thread = threading.Thread(target=work, name=f"live-{pid}", daemon=True)
        _streams[pid] = thread
        thread.start()


def submit_prepare(pid: str) -> None:
    _guarded(pid, lambda: pipeline.prepare_project(pid))


def submit_process(pid: str) -> None:
    _guarded(pid, lambda: pipeline.process_project(pid))


def submit_rerender(clip_id: str, payload: Dict[str, Any], *,
                    agent_edit: bool = False, undo: Dict[str, Any] | None = None,
                    restore_project_status: Optional[str] = None,
                    restore_project_stage: Optional[str] = None) -> bool:
    with _lock:
        if clip_id in _clip_running:
            return False
        previous = db.get_clip(clip_id)
        if not previous or (agent_edit and previous["status"] != "ready"):
            return False
        db.update_clip(clip_id, status="rendering", error=None)
        _clip_running.add(clip_id)
        _clip_running_projects[clip_id] = str(previous["project_id"])

    def _restore_project_if_needed(failed_msg: Optional[str] = None) -> None:
        if not restore_project_status:
            return
        pid = str(previous["project_id"])
        if any(p == pid for cid, p in _clip_running_projects.items() if cid != clip_id):
            return
        cur = db.get_project(pid)
        if cur and cur.get("status") == "running":
            if failed_msg and restore_project_status != "done":
                db.update_project(pid, status="error", error=failed_msg[:400], stage="Ошибка")
            else:
                db.update_project(pid, status=restore_project_status,
                                  stage=restore_project_stage or "Готово", error=None)

    def work() -> None:
        err_msg: Optional[str] = None
        try:
            with _clip_gate:
                if undo is None:
                    rendered = pipeline.rerender_clip(clip_id, payload)
                else:
                    rendered = pipeline.rerender_clip(clip_id, payload, restore=undo["before"])
                with _lock:
                    if undo is not None:
                        _agent_edits.remove(undo)
                        prior = next((event for event in reversed(_agent_edits)
                                      if event["id"] == clip_id), None)
                        if prior:
                            prior["revision"] = rendered["updated_at"]
                    elif agent_edit:
                        last = next((event for event in reversed(_agent_edits)
                                     if event["id"] == clip_id), None)
                        if last is not None and last["revision"] != previous["updated_at"]:
                            _agent_edits[:] = [event for event in _agent_edits if event["id"] != clip_id]
                        _agent_edits.append({
                            "id": clip_id, "project_id": previous["project_id"],
                            "revision": rendered["updated_at"],
                            "before": {key: previous[key] for key in ("settings", "words")},
                        })
                        # Не удаляем запись, пока по ней выполняется отмена.
                        while len(_agent_edits) > _AGENT_HISTORY_LIMIT:
                            oldest = next((i for i, event in enumerate(_agent_edits)
                                           if event["id"] not in _clip_running), None)
                            if oldest is None:
                                break
                            _agent_edits.pop(oldest)
                    else:
                        _agent_edits[:] = [event for event in _agent_edits if event["id"] != clip_id]
        except Exception as exc:  # noqa: BLE001
            err_msg = str(exc)[:500]
            previous_ready = (previous["status"] == "ready" and previous.get("path")
                              and Path(previous["path"]).is_file())
            db.update_clip(clip_id, status="ready" if previous_ready else "error", error=err_msg)
            with _lock:
                latest = next((event for event in reversed(_agent_edits)
                               if event["id"] == clip_id), None)
                if latest and latest["revision"] == previous["updated_at"] and previous_ready:
                    current = db.get_clip(clip_id)
                    if current and current["settings"] == previous["settings"] and current["words"] == previous["words"]:
                        latest["revision"] = current["updated_at"]
            print(f"[render] пересборка клипа {clip_id}: {exc}", file=sys.stderr, flush=True)
        finally:
            with _lock:
                _clip_running.discard(clip_id)
                _clip_running_projects.pop(clip_id, None)
                _restore_project_if_needed(err_msg)

    _clip_pool.submit(work)
    return True


def undo_agent_edit(project_id: str, clip_id: str | None = None) -> tuple[bool, str]:
    """Ставит откат последней успешной правки агента в очередь рендера."""
    with _lock:
        event = next((record for record in reversed(_agent_edits)
                      if record["project_id"] == project_id
                      and (clip_id is None or record["id"] == clip_id)), None)
        if event is None:
            return False, "Готовых правок агента для отмены пока нет."
        cid = event["id"]
        if cid in _clip_running:
            return False, "Клип ещё пересобирается. Дождитесь завершения и повторите отмену."
        clip = db.get_clip(cid)
        if not clip or clip["status"] != "ready" or clip["updated_at"] != event["revision"]:
            return False, "Клип изменился после правки агента. Отмена недоступна."
        if not submit_rerender(cid, {}, undo=event):
            return False, "Не удалось поставить отмену в очередь."
        return True, f"Возвращаю предыдущую версию клипа №{clip['idx'] + 1}."
