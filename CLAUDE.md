# CLAUDE.md

## Project
`mediaconv.py` is a single-file CLI that batch-converts images (Pillow, pillow-heif), video and audio (ffmpeg subprocess). See README.md for usage.

## Commands
- Run: `python mediaconv.py -c --png --dry-run`
- No test suite or dependencies file. To test, make a scratch directory with sample files and run the script there. With `--log`, logs are written to the current directory, so never test in the repo root.

## Architecture
- `MODES` maps each format flag to (source suffixes, kind, help text); argparse flags are generated from it. `target_suffix()` and `convert()` decide output extension and handler. To add a format, add a `MODES` entry (and to `IMAGE_GROUP`/`VIDEO_GROUP`/`AUDIO_GROUP` if it belongs in a selector).
- `expand_modes()` expands selectors and validates switch combinations. `ensure_prerequisites()` runs after file collection and only checks what the found files need.
- Flow: `main` -> `build_tasks` -> `collect` (case-insensitive) -> `ensure_prerequisites` -> `run_pipeline` (images, then ffmpeg jobs, via `ThreadPoolExecutor`) -> `worker` -> converter.
- Converters (`jpeg_rename`, `image_convert`, `video_convert`, `audio_convert`) share `begin()` for target/skip/dry-run handling and `run_ffmpeg()` for progress. They return `ok`, `dryrun`, `skipped` or `error`, and handle their own exceptions.

## Invariants (do not break)
- Never delete a source (`-D`) unless the conversion verifiably succeeded, including the ffmpeg return code.
- Write output to a `.part` file, then `os.replace` it. Clean up the `.part` on failure.
- All name reservation goes through `unique()` or `resolve_target()` under `lock`. Shared state (`used_targets`, counters, log files) is touched only under `lock`.
- `--overwrite` and `--skip-existing` are mutually exclusive. Default is conflict-safe `_NNN` suffixes.
- Prerequisites are never installed without confirmation (`confirm()`); only `--yes` bypasses the prompt, and `--no-install` forbids it.
- Switch convention: long options `--kebab-case`; every single-letter switch must be registered in both cases (`-c`, `-C`).
- Bump `VERSION`/`VERSION_DATE` (and the docstring) on behavior changes. `ShortUsageParser.error` prints version + short usage on bad command lines. Keep README's tables and the `--help` text in sync.
- Keep help and console output ASCII-only (Windows cp1252 consoles crash on `→`).

## Style
- Plain Python stdlib plus Pillow. No new dependencies without asking.
- Section banners (`# ====`) separate sections. Keep the module docstring and `--help` text in sync with the flags.
- Update the version line in the docstring when behavior changes.

## Console output and logging
- All console output while workers run goes through `progress.say()` / `progress.update()` / `progress.finish_file()` so the live status line is not corrupted. Do not call bare `print` from worker code.
- `record()`: errors go to stderr and always to `mediaconv.errors.log` (created lazily on first error); other messages go to `mediaconv.log` only with `--log`.
- `--remux` copies only codecs in `MP4_VIDEO`/`MP4_AUDIO` (decided via `probe_codecs`), otherwise it re-encodes.
- ffmpeg percent comes from `ffmpeg -progress pipe:1` (`out_time_us`) divided by the `ffprobe` duration.
