# mediaconv

A single-file Python tool that batch-converts images, video and audio in the current directory (or a whole tree) to widely compatible formats, with live progress and safe file handling.

## Formats

| Kind  | Switches | Output |
|-------|----------|--------|
| Image | `--jpeg` (rename only), `--png`, `--webp`, `--bmp`, `--tiff`, `--ico`, `--heic` | `.jpg` |
| Image | `--jpg` (needs `--image-to webp`) plus any of the above except `--webp` | `.webp` |
| Video | `--webm`, `--wmv`, `--avi`, `--flv`, `--mov`, `--mkv`, `--mpg`, `--3gp`, `--m4v`, `--ts`, `--gif` | `.mp4` |
| Audio | `--wma`, `--flac`, `--wav`, `--ogg`, `--m4a` | `.mp3` (or `.m4a` with `--audio-to aac`) |
| Video → audio | `--mp4` and any video switch, with `--extract-audio` | `.mp3` / `.m4a` |

`--images`, `--videos` and `--audio` select every format of that kind. `--videos` includes `--ts`; since `.ts` is also TypeScript, prefer a narrower selector when a tree contains TypeScript sources (a non-video `.ts` fails to convert and its source is kept).

Multi-frame inputs: `--tiff` converts the first page; `--gif` is treated as a video and becomes an MP4 (no audio).

## Requirements

- Python 3.8+
- [Pillow](https://pypi.org/project/Pillow/) for images
- [pillow-heif](https://pypi.org/project/pillow-heif/) for `--heic`
- [ffmpeg](https://ffmpeg.org/) (with `ffprobe`) for video and audio

Only what the selected files actually need is checked. If something is missing, the tool shows the exact install command and **asks before running it**:

- Pillow / pillow-heif: `python -m pip install ...`
- ffmpeg: `winget` (Windows), `brew` (macOS), or `apt-get` / `dnf` / `pacman` / `zypper` via `sudo` (Linux)

Use `--yes` to approve installs up front (for scripts), `--no-install` to never offer, and `--check` to print prerequisite status (including available hardware encoders) and exit. In a non-interactive session without `--yes`, nothing is installed. Dry runs skip the check.

## Usage

```
python mediaconv.py (-c | -r) [format switches] [options]
```

| Group | Switch | Meaning |
|-------|--------|---------|
| Scope (one required) | `-c`, `--current` | current directory only |
| | `-r`, `--recursive` | include subdirectories |
| Image | `--image-to {jpg,webp}` | target format (default `jpg`) |
| | `--quality N` | 1-100 (default 95 JPG, 90 WebP) |
| | `--max-size PX` | shrink so the longest side is at most PX |
| | `--strip-metadata` | drop EXIF/GPS (kept by default) |
| Video | `--h265` | H.265/HEVC instead of H.264 |
| | `--crf N` | quality, lower is better (default 23 H.264, 28 H.265) |
| | `--preset NAME` | software encoder speed (default `medium`) |
| | `--hw {nvenc,qsv,amf}` | hardware encoder |
| | `--remux` | copy streams, no re-encode, for mkv/mov/ts/m4v/3gp/mpg |
| Merge | `-m`, `--merge` | join the videos of each folder into `<folder>_merged.mp4` (see below) |
| Audio | `--audio-to {mp3,aac}` | target (default `mp3`; `aac` writes `.m4a`) |
| | `--abitrate RATE` | bitrate (default `192k`) |
| | `--extract-audio` | video sources produce audio files |
| Behavior | `-o`, `--overwrite` | overwrite existing output |
| | `-s`, `--skip-existing` | skip when output exists |
| | `-d`, `--delete-source` | delete source after a successful conversion (with `--merge`: after a warning and confirmation) |
| | `-n`, `--dry-run` | simulate only |
| | `-l`, `--log` | write the activity log `mediaconv.log` (errors are always logged, see below) |
| Prerequisites | `-y`, `--yes` / `--no-install` / `--check` | see above |
| Help | `-h`, `--help` | full switch list, with version |
| | `-v`, `--version` | show version and exit |

Every single-letter switch works in either case (`-h` = `-H`, `-r` = `-R`, `-v` = `-V`, …).

`--overwrite` and `--skip-existing` are mutually exclusive. With neither, name conflicts get `_001`, `_002`, … appended.

### Examples

```
python mediaconv.py -c --png --webp                  # PNG/WebP -> JPG here
python mediaconv.py -r --images --image-to webp      # every image -> WebP, recursive
python mediaconv.py -r --videos --remux -d           # videos -> MP4, copy streams when possible
python mediaconv.py -r --mkv --h265 --crf 26         # MKV -> H.265 MP4
python mediaconv.py -c --audio --audio-to aac        # audio -> .m4a
python mediaconv.py -c --mp4 --extract-audio         # MP3 from MP4 files
python mediaconv.py -r --videos --merge              # one MP4 per folder
python mediaconv.py -r --heic --max-size 2048 -n     # preview HEIC -> resized JPG
```

## Behavior

- Extensions match case-insensitively.
- Images: EXIF orientation is applied, EXIF/GPS is preserved unless `--strip-metadata`, transparency becomes white for JPG and is kept for WebP.
- Video: `libx264` (or `libx265`), AAC audio, `yuv420p`, `+faststart`, subtitles dropped, odd dimensions rounded to even.
- `--merge` joins the videos of each folder (with `-r`, each subfolder separately) into `<folder>_merged.mp4` in that folder, in natural name order (`clip2` before `clip10`). `--videos` then also includes `.mp4`; earlier `*_merged.mp4` outputs are ignored. All clips are scaled to fit the largest frame (by area) keeping their aspect ratio and padded with black where needed, in one or both dimensions; frame rate is matched to the highest, audio is converted to a common format, and clips without audio get silence. Folders with a single video are skipped. Merging always re-encodes (no `--remux`) and needs `ffprobe`. Files ffprobe cannot read as video are left out of the merge and kept.
- `--merge` with `-d` prints a warning with the number of source files and asks for confirmation (`--yes` skips the question); declining cancels the run. Sources are deleted only after their folder merged successfully.
- `--remux` copies a file's video (and audio, when MP4-compatible) without re-encoding. If the codecs do not fit MP4, or the copy fails, that file is re-encoded instead. This needs `ffprobe`.
- Output is written to a `.part` file and moved into place only on success, so failures never leave truncated results.
- The source is deleted (`-d`) only after a verified successful conversion; a non-zero ffmpeg exit counts as failure.
- A simple scheduler picks worker counts from system load (a neutral default is used on Windows). Images run first, then ffmpeg jobs.

## Output and logs

- Console: per-type file counts, a `Processing:` line per file, and a final summary. On a terminal a live status line shows `x of y files processed` plus the percent done of each active ffmpeg job (percent needs `ffprobe`). With `--merge` the count is of merge files (one per folder) and each active job also shows which input is being added, e.g. `clips_merged.mp4  40% (adding file 2/5)`. When output is redirected, one `[x/y] name` line is printed per finished file instead.
- Errors are always printed to stderr.
- `mediaconv.errors.log` is written whenever an error occurs, with or without `-l`. It is created on the first error, so it exists only if something failed.
- With `-l`, `mediaconv.log` (summary and every action) is also written. Without `-l` it is not created.
- A malformed command line prints the version, a short usage line and the error; `-h` prints the full help.

Exit codes: `0` success, `1` conversion failures or missing prerequisites, `2` invalid arguments.

## Man page

`mediaconv.1` is the manual page. View it with `man ./mediaconv.1`, or install it with `install -Dm644 mediaconv.1 /usr/local/share/man/man1/mediaconv.1`. Keep it in sync with the switches in `--help` when they change.

## Upgrading from v4

All switches now follow one convention: long options use `--`, with short aliases for the common ones.

| Old | New |
|-----|-----|
| `-C`, `-R` | still valid (`-c`/`-C`/`--current`, `-r`/`-R`/`--recursive`) |
| `-jpeg -png -webp -webm -wmv -avi -flv` | `--jpeg --png --webp --webm --wmv --avi --flv` |
| `-O`, `-S`, `-D` | still valid (also `-o`, `-s`, `-d`, `--overwrite`, `--skip-existing`, `--delete-source`) |
| `-L` | still valid (also `-l`, `--log`); now only controls the activity log |
| `--dryrun` | `-n`/`--dry-run` |
| `-h -H -?` | `-h`/`-H`/`--help`; also new `-v`/`-V`/`--version` |
