# Custom Changes Log

Track all personal modifications made on top of `upstream/main` (`vanwild11/viralcut`) here.
When upstream makes major refactors, an LLM agent can use this log to re-apply or adapt our features cleanly onto the new `main`.

## Setup

- Initial fork setup on branch `custom` (`AGENTS.md`, `CUSTOM_CHANGES.md`).

## Larger Default Whisper Model (`medium`)
- **Goal**: Use a larger, more accurate Whisper model (`medium` instead of `small`) by default when `VIRALCUT_WHISPER` is not explicitly set.
- **New files**: `viralcut/custom_whisper.py` (`DEFAULT_WHISPER_MODEL = "medium"`, `resolve_whisper_model()`).
- **Modified upstream files**: `viralcut/config.py` (minimal import and call `WHISPER_MODEL = resolve_whisper_model()`).
- **Verification**:
  - `python -c "from viralcut import config; assert config.WHISPER_MODEL == 'medium'"`
  - With `VIRALCUT_WHISPER=large-v3`, verify `config.WHISPER_MODEL == 'large-v3'`.

<!-- Template for future changes:
## [Feature / Fix Name]
- **Goal**: Why this change exists and expected behavior.
- **New files**: `path/to/new_file.py`
- **Modified upstream files**: `path/to/upstream_file.py` (minimal hook/import)
- **Verification**: Command or steps to confirm it works.
-->
