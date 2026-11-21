# Custom Changes Log

Track all personal modifications made on top of `upstream/main` (`vanwild11/viralcut`) here.
When upstream makes major refactors, an LLM agent can use this log to re-apply or adapt our features cleanly onto the new `main`.

## Setup

- Initial fork setup on branch `custom` (`AGENTS.md`, `CUSTOM_CHANGES.md`).

## Larger Default Whisper Model + Windows CUDA Acceleration
- **Goal**: Tune the fork for the personal Windows PC (Ryzen 7 5700X3D, RTX 4060 8 GB VRAM) with GPU-accelerated Whisper (`cuda` `float16` → `int8_float16` → `cpu` fallback) and `large-v3` as the default model.
- **New files**:
  - `viralcut/custom_whisper.py`: `DEFAULT_WHISPER_MODEL = "large-v3"`, `resolve_whisper_model()`, `register_cuda_dlls()` (registers pip `nvidia/*/bin` DLL directories on Windows), and `whisper_candidates()`.
  - `requirements-custom.txt`: extends `requirements-desktop.txt` with `nvidia-cublas-cu12` and `nvidia-cudnn-cu12`.
- **Modified upstream files**:
  - `viralcut/config.py`: minimal import and call `WHISPER_MODEL = resolve_whisper_model()`.
  - `viralcut/transcribe.py`: replaces inline device candidate list with `whisper_candidates(long_audio, WHISPER_DEVICE, WHISPER_COMPUTE)`.
  - `run_windows.bat`: installs `requirements-custom.txt` instead of `requirements-desktop.txt`.
  - `README.md`: added personal fork & hardware-tuning notice at the top.
- **Verification**:
  - `.\.venv-win\Scripts\python.exe -c "from viralcut import config; assert config.WHISPER_MODEL == 'large-v3'"`
  - Verify `whisper_candidates(False, "auto", "int8")` begins with `("cuda", "float16")` and CUDA DLLs load cleanly.
  - Run `.\.venv-win\Scripts\python.exe -m unittest discover -s tests`.

## YouTube Quality Selection & Section Download Fix (1080p in Eco Mode)
- **Goal**: Fix two bugs causing 1080p YouTube selections in eco/segments mode to silently fall back to 360p (format `18`) and upscale during render:
  1. `downloader.download_section()` hardcoded `player_client: ["android", "tv"]` instead of probing clients starting from `default`.
  2. `downloader._pick_format()` ranked `avc1`/`mp4`/muxed-audio ahead of `height`, allowing 360p muxed streams to beat 1080p adaptive streams.
- **New files**:
  - `viralcut/custom_downloader.py`: `pick_best_format()` (strict height-first ranking preferring direct HTTPS over HLS at equal height/codec) and `download_section_smart()` (probes YouTube clients via `_youtube_info`, records real probed dimensions and fallback reason, and notifies `custom_debug`).
  - `tests/test_custom_features.py`: unit tests for format selection, section download client fallback, and debug telemetry.
- **Modified upstream files**:
  - `viralcut/downloader.py`: delegates `_pick_format()` and `download_section()` (adding optional `meta_out`) to `custom_downloader`.
  - `viralcut/pipeline.py`: passes `meta_out` and `force_hd` in `_process_segments()` and persists source quality metadata via `db.merge_meta()`.
- **Verification**:
  - Run `.\.venv-win\Scripts\python.exe -m unittest discover -s tests -v`.
  - Download a 3s range of `https://youtu.be/9-2Gz9343hQ` with `download_section(..., max_height=1080, meta_out=meta)` and assert probed height `== 1080` and `fallback == False`.

## Native Live Debug Inspector Panel
- **Goal**: Add a native iOS dark-glass live inspector (`Инспектор`, `Ctrl+Shift+D`, toggled from `#health` / `.side-foot`) showing live GPU/VRAM, process/system CPU & RAM, Whisper model/device/download speed (including live NTFS `.incomplete` blob growth), YouTube client/format/upscale warnings, active project stage timer, and running `ffmpeg` child processes.
- **New files**:
  - `viralcut/custom_debug.py`: thread-safe telemetry store + cached 1s hardware sampler (typed Win32 ctypes CPU/RAM/file-size + `nvidia-smi`).
  - `web/custom_debug.css`: glass drawer/grid styling matched to `web/styles.css` variables.
  - `web/custom_debug.js`: self-contained UI + non-overlapping polling only while open and visible.
- **Modified upstream files**:
  - `viralcut/main.py`: adds `GET /api/debug` returning `custom_debug.snapshot()`.
  - `viralcut/jobs.py`: adds `active_project_ids()` helper.
  - `viralcut/downloader.py`, `viralcut/transcribe.py`, `viralcut/render.py`: minimal hooks reporting download progress, Whisper device/status, and `ffmpeg` child PIDs/progress.
  - `web/index.html`: includes `/static/custom_debug.css?v=1` and `/static/custom_debug.js?v=1`.
- **Verification**:
  - `.\.venv-win\Scripts\python.exe -c "from viralcut import main; d = main.debug_status(); assert d['server']['ram_mb'] > 0 and d['gpu']['available']"`
  - `.\.venv-win\Scripts\python.exe -m unittest discover -s tests -v`

## Auto-Update Check Disabled by Default + UI Toggle
- **Goal**: Upstream checks `vanwild11/viralcut` GitHub releases on startup. Disable upstream update checks by default on this personal fork and expose an explicit UI toggle (`Автообнова: выкл/вкл`) in `.side-foot` and the Inspector footer.
- **New files**:
  - `viralcut/custom_updater.py`: `DEFAULT_UPDATE_CHECK = False` and `resolve_update_enabled()` (honors `VIRALCUT_UPDATE_CHECK` and persisted `data/update.json` toggle).
- **Modified upstream files**:
  - `viralcut/updater.py`: delegates `enabled()` to `custom_updater.resolve_update_enabled()` and makes `_save()` atomic via `.tmp` replace.
  - `web/custom_debug.js`, `web/custom_debug.css`: renders `Автообнова: выкл` / `Автообнова: вкл` toggle synced with `/api/update` and `/api/update/settings`.
- **Verification**:
  - `.\.venv-win\Scripts\python.exe -c "from viralcut import updater; print(updater.payload()['enabled'])"` (returns `False` cleanly without hitting GitHub when unconfigured).
  - Run `.\.venv-win\Scripts\python.exe -m unittest discover -s tests -v`.

## Audit Fixes: Stability, Resource Leaks, Eco-Mode Rerender & Preview Polish
- **Goal**: Fix critical rendering/concurrency bugs, resource/disk/VRAM leaks, and UI preview glitches discovered during full audit.
- **Modified custom files**:
  - `viralcut/custom_downloader.py`: propagates `Cancelled` in `download_section_smart()`; adds `rerender_section_clip()` so eco-mode (`segments=True`) clips can be re-rendered cleanly by re-fetching only their clip range.
  - `viralcut/custom_fonts.py`: protects `sync()` with `_SYNC_LOCK`, skips re-copying unchanged files on Windows, copies atomically via `.tmp`, and closes `TTFont` via `with`.
  - `viralcut/custom_debug.py`, `web/custom_debug.js`, `web/custom_debug.css`: hardens `_init_win32()` lock, guards `_MODEL_DL_PREV` and `.incomplete` `.stat()` races, cools down missing `nvidia-smi`, rounds GPU temperature, displays resolved idle Whisper device (`cuda`/`float16`), aborts in-flight polls on close/pause, and hides closed drawer from focus/screen-readers (`visibility: hidden`, `inert`, `aria-hidden`).
- **Modified upstream files**:
  - `viralcut/db.py`: turns `connect()` into a `@contextmanager` that always calls `conn.close()` in `finally:` (fixes SQLite handle leak on every poll).
  - `viralcut/render.py`: extracts `_run_render_proc()` so hardware encoder fallback to `libx264` preserves tail flags (`-r 30 ... <out_path>`) and drains `stdout` without pipe deadlock.
  - `viralcut/transcribe.py`: shares a single model cache slot on Windows/Linux (prevents double ~6 GB `large-v3` VRAM allocation), always deletes `audio16k.wav` and `whisper_chunks_*` in `finally:`, and propagates `Cancelled`.
  - `viralcut/downloader.py`, `viralcut/pipeline.py`, `viralcut/jobs.py`, `viralcut/live.py`, `viralcut/reframe.py`: propagates `Cancelled`, adds `_CANCEL_LOCK`, serializes `retry` cancellation via `jobs.submit_retry()`, cleans temporary `sections/` and `win_*.wav` files in `finally:`, avoids leaking `_STOP_EVENTS` on non-live projects, caches Haar cascade XML, and enables eco-mode rerender via `custom_downloader.rerender_section_clip()`.
  - `viralcut/main.py`: switches `/static/` cache header to `no-cache`, streams font upload reads within `MAX_FONT_BYTES`, cleans `/api/upload` workdir on any error, hides audio-only `source` from `source_url`, validates `clip_rerender` upfront, and coordinates safe delete/retry cancellation.
  - `web/index.html`, `web/app.js`, `web/styles.css`: releases `<video>` decoders before DOM replacement, prevents double-render & `preload="metadata"` `206` storm on project open, caches posters by `updated_at`, fixes clip editor relative seeking & wide numeric time fields, puts `Скачать MP4` + `SRT` side-by-side, and adds click-to-pause, hook preview, aspect sync, `ResizeObserver`, and dirty-state protection in template preview.
- **Verification**:
  - `.\.venv-win\Scripts\python.exe -m unittest discover -s tests -v`.

## Disk Accounting & Dual Cleanup Controls in Inspector
- **Goal**: Show per-category disk usage (`clips`, `temp`, `sources`, `models`) and provide two safe cleanup controls in the Inspector (`Очистить временные` and `Удалить исходники`) without touching Whisper models, finished clips, `review` projects, or active jobs.
- **New files**:
  - `viralcut/custom_cleanup.py`: cached `storage_summary()`, active-project protection, and `run_cleanup(scope="temp" | "sources")`.
- **Modified custom files**:
  - `viralcut/custom_debug.py`: attaches `"storage": custom_cleanup.storage_summary()` to `snapshot()`.
  - `web/custom_debug.js`, `web/custom_debug.css`: adds the `Диск и очистка` card with both buttons and confirmation prompt for `sources`.
  - `tests/test_custom_features.py`: adds `CustomCleanupTest`.
- **Modified upstream files**:
  - `viralcut/main.py`: adds `CleanupIn` and `POST /api/debug/cleanup`.
  - `web/index.html`: bumps `custom_debug` asset query version to `?v=3`.
- **Verification**:
  - `.\.venv-win\Scripts\python.exe -m unittest discover -s tests -v`.
  - Open `Инспектор` at `http://localhost:8765/` and verify `Диск и очистка` card renders accurate sizes and active states for both buttons.

<!-- Template for future changes:
## [Feature / Fix Name]
- **Goal**: Why this change exists and expected behavior.
- **New files**: `path/to/new_file.py`
- **Modified upstream files**: `path/to/upstream_file.py` (minimal hook/import)
- **Verification**: Command or steps to confirm it works.
-->
