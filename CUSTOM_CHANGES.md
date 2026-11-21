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

<!-- Template for future changes:
## [Feature / Fix Name]
- **Goal**: Why this change exists and expected behavior.
- **New files**: `path/to/new_file.py`
- **Modified upstream files**: `path/to/upstream_file.py` (minimal hook/import)
- **Verification**: Command or steps to confirm it works.
-->
