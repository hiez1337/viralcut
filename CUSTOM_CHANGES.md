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

<!-- Template for future changes:
## [Feature / Fix Name]
- **Goal**: Why this change exists and expected behavior.
- **New files**: `path/to/new_file.py`
- **Modified upstream files**: `path/to/upstream_file.py` (minimal hook/import)
- **Verification**: Command or steps to confirm it works.
-->
