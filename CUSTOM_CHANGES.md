# Custom Changes Log

*[Русская версия / Russian translation: [CUSTOM_CHANGES_ru.md](CUSTOM_CHANGES_ru.md)]*

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

## Complete Fork README Rewrite & GitHub Metadata Cleanup
- **Goal**: Rewrite `README.md` from scratch for the `hiez1337/viralcut` fork so visitors and users are not misled by upstream release downloads, `@ivanAI22` badges/banners, `vanwild11.github.io` links, `small` model defaults, or outdated instructions. Update GitHub repository metadata (`default_branch=custom`, fork-specific description, clear upstream homepage URL).
- **Modified files**:
  - `README.md`: full rewrite covering fork differences, CUDA/`large-v3`/NVENC defaults, Inspector & disk cleanup, disabled-by-default auto-updates, accurate environment variables, API endpoints, source-run setup (`run_windows.bat` / `./run.sh`), branch workflow, and clear separation from upstream `vanwild11/viralcut`.
  - `CUSTOM_CHANGES.md`: documented this README & repository metadata update.
- **Verification**:
  - Check `README.md` contains no stale `1.3.0` or `@ivanAI22` references and links only valid local paths and `hiez1337/viralcut` (with `vanwild11/viralcut` referenced solely for upstream attribution/updater explanation).
  - Verify `gh repo view hiez1337/viralcut --json defaultBranchRef,description,homepageUrl` shows `custom`, updated fork description, and empty `homepageUrl`.

## Whisper `small` Default + UI Model Switcher (`small` / `medium` / `large-v3`) & Download Progress
- **Goal**: Restore `small` as the default Whisper model (friendly for CPU and Lite builds), let users inspect model sizes/requirements upfront, switch between `small`, `medium`, and `large-v3` in the main UI (persisted to `data/whisper.json`), pre-download models with a live progress bar/speed/ETA, and safely fall back to CPU when CUDA/cuDNN DLLs are absent.
- **New files**:
  - `web/custom_whisper.css`, `web/custom_whisper.js`: self-contained model picker cards, status badges (`Готова` / `↓ ~XXX МБ` / `Загрузка XX%` / `Ошибка`), pre-download button, live progress bar, runtime GPU/CPU chip & warning note.
- **Modified custom files**:
  - `viralcut/custom_whisper.py`: sets `DEFAULT_WHISPER_MODEL = "small"`, adds `WHISPER_CATALOG`, `whisper.json` persistence (`resolve_whisper_model()`, `get_active_model()`, `set_active_model()`), `resolve_model_ref()`, safe `_cuda_dlls_available()` / `cuda_diagnostics()` check (including `sys._MEIPASS/nvidia/*/bin` and system `CUDA_PATH`), deduplicated `ensure_model_downloaded()` / `start_model_download()` with standalone `ProgressTqdm`, and `models_payload()`.
  - `viralcut/custom_debug.py`, `web/custom_debug.js`: tracks active model dynamically while idle, enriches download telemetry with `pct` / `eta_s` / `download_model`, and displays `cuda_note` if CUDA DLLs are missing.
  - `tests/test_custom_features.py`: adds `CustomWhisperTest`.
- **Modified upstream files**:
  - `viralcut/transcribe.py`: delegates `model_cached()` to `custom_whisper.is_model_cached()`, tracks loaded slot model name (`_MODEL_NAME`/`_MODEL_LONG_NAME`) to reload on model change, calls `ensure_model_downloaded()`, and supports `model_name` argument.
  - `viralcut/pipeline.py`, `viralcut/live.py`: adds `whisper_model` option in `DEFAULT_OPTIONS` / `normalize_options()`, reports live model download progress before transcription, and passes `model_name` to `transcribe.transcribe()`.
  - `viralcut/main.py`: adds `whisper_model` to `/api/health` and endpoints `GET /api/whisper/models`, `POST /api/whisper/model`, `POST /api/whisper/download`.
  - `web/index.html`, `web/app.js`: mounts `#vcWhisperSlot` in `heroHTML()`/`bindHero()`, passes `whisper_model` in `heroOptions()`, and shows active model in `#healthText`.
- **Verification**:
  - `.\.venv-win\Scripts\python.exe -m unittest discover -s tests -v`.

## Windows Lite Release Pipeline (No CUDA DLLs) & Beginner README Guide
- **Goal**: Build and publish a compact Windows x64 Lite release (`ViralCut-*-windows-x64-lite.zip`) without bundled CUDA/cuDNN DLLs or embedded Whisper models, document all Lite limitations clearly, and provide beginner-friendly instructions with direct links to both the Lite release and `custom.zip` + `install_windows.bat`/`run_windows.bat` for full GPU acceleration.
- **New files**:
  - `ViralCut-lite.spec`: PyInstaller spec for Windows Lite build excluding `nvidia` and filtering out `cublas`/`cudnn`/`cudart`/`nvrtc` DLLs and `whisper-small` while keeping all `viralcut.*` submodules.
  - `build_windows_lite.ps1`: isolated `.venv-build` setup from `requirements-desktop.txt`, unit test gate, PyInstaller build, DLL/model absence assertions, headless `ViralCut.exe` smoke test, ZIP packaging, and `SHA256SUMS.txt`.
  - `.github/workflows/custom-release.yml`: manual `workflow_dispatch` GitHub Actions workflow mirroring the Lite build.
  - `release/fork-v1.4.1-1/RELEASE_NOTES.md`, `release/fork-v1.4.1-1/SHA256SUMS.txt`: release notes and checksums for tag `fork-v1.4.1-1`.
- **Modified files**:
  - `README.md`, `CUSTOM_CHANGES.md`.
- **Verification**:
  - `powershell -ExecutionPolicy Bypass -File .\build_windows_lite.ps1 -Tag fork-v1.4.1-1 -Version 1.4.1-fork.1`
  - `gh release view fork-v1.4.1-1 --repo hiez1337/viralcut --json tagName,url,isLatest,assets`

## Russian Changelog (`CUSTOM_CHANGES_ru.md`) & GitHub Issues / Discussions Enablement
- **Goal**: Enable GitHub Issues and Discussions on `hiez1337/viralcut` via GitHub CLI so users can report bugs, suggest features, and ask questions. Provide a complete Russian translation of the custom changes log (`CUSTOM_CHANGES_ru.md`) alongside `CUSTOM_CHANGES.md`.
- **New files**:
  - `CUSTOM_CHANGES_ru.md`: full Russian translation and duplicate of `CUSTOM_CHANGES.md`.
- **Modified files**:
  - `CUSTOM_CHANGES.md`: cross-linked Russian translation.
  - `README.md`: added direct links to both Russian and English changelogs in the navigation bar and repository structure.
- **Verification**:
  - `gh repo view hiez1337/viralcut --json hasIssuesEnabled,hasDiscussionsEnabled` returns `true` for both.
  - Verify links between `README.md`, `CUSTOM_CHANGES.md`, and `CUSTOM_CHANGES_ru.md`.

## Upstream `v1.4.2` Sync & Automated Fork CI (`custom-ci.yml`)
- **Goal**: Merge upstream `vanwild11/viralcut` `v1.4.2` render fixes (8-frame `_hw_works()` hardware encoder probe, clean `_compose()` / `_run_encode()` fallback to `libx264`, `/api/health` `"hw"` field, detailed clip failure reason in `pipeline.py`, and `tests/test_render.py`) into the fork while preserving all custom features and adding automated CI test runs on `custom` PRs.
- **New files**:
  - `.github/workflows/custom-ci.yml`: runs `python -m unittest discover -s tests -v` on `windows-latest` for every push/PR targeting `custom`.
- **Modified / merged files**:
  - `viralcut/render.py`: adopted upstream's `_hw_works()`, `_video_args()`, `_run_encode()`, and `_compose()` structure with minimal `custom_debug` process tracking and stream cleanup hooks.
  - `viralcut/main.py`: combined upstream's `"hw": render.hw_encoder()` in `/api/health` with our `"whisper_model"` and custom endpoints.
  - `viralcut/pipeline.py`: adopted upstream's detailed failure reason when `rendered == 0`.
  - `tests/test_custom_features.py`: updated `RenderFallbackTest` for `_run_encode()` and `custom_debug` process unregister verification.
  - `README.md`, `CUSTOM_CHANGES.md`, `CUSTOM_CHANGES_ru.md`: updated base version to `1.4.2` and documented CI & `tests/test_render.py`.
- **Verification**:
  - `.\.venv-win\Scripts\python.exe -m unittest discover -s tests -v` (all tests including upstream `tests/test_render.py` pass).
  - Verify green `Fork CI (Tests)` check on PR into `custom`.

## Upstream `v1.4.3` Sync (Smooth Reframing, Eco Full HD Rebuild & Direct-URL Sections)
- **Goal**: Merge upstream `vanwild11/viralcut` `v1.4.3` (smooth 3 fps + median-5 + deadband + Douglas–Peucker reframing fixing long-clip FFmpeg failures, one-click 1080p rebuild from eco mode without re-running Whisper, and direct-URL ffmpeg extraction/cutting) while keeping our fork's `custom_downloader`, race-safe retry serialization, and Windows subprocess flags.
- **Modified / merged files**:
  - `viralcut/reframe.py`, `viralcut/db.py`, `web/app.js`, `tests/test_reframe.py`, `tests/test_render.py`: clean upstream merge preserving `_haar_cascade()` caching.
  - `viralcut/downloader.py`: added direct-URL early path (`_extract_direct_audio` / `_cut_direct_section` with `subprocess_kwargs()` and `meta_out` probing) ahead of `custom_downloader.download_section_smart()`.
  - `viralcut/main.py`, `viralcut/jobs.py`, `viralcut/pipeline.py`: combined upstream's `force_hd` + `segments_mode` 1080p rebuild (`db.reset_clips_pending`) with race-safe `jobs.submit_retry(..., segments_hd=...)` and `meta_out` tracking.
  - `README.md`, `CUSTOM_CHANGES.md`, `CUSTOM_CHANGES_ru.md`: updated base version to `1.4.3`.
- **Verification**:
  - `.\.venv-win\Scripts\python.exe -m unittest discover -s tests -v` (including `LongClipRenderTest` 36s reframed render).

<!-- Template for future changes:
## [Feature / Fix Name]
- **Goal**: Why this change exists and expected behavior.
- **New files**: `path/to/new_file.py`
- **Modified upstream files**: `path/to/upstream_file.py` (minimal hook/import)
- **Verification**: Command or steps to confirm it works.
-->
