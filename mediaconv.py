#!/usr/bin/env python3
"""
mediaconv.py
Version: 2026-11-05 v5.7

Adaptive media converter (images, video, audio).

- --m4v: a .m4v that already is a valid MP4 (ffprobe: MP4 container, MP4 codecs) is
  renamed to .mp4 (copied unless -d) instead of re-encoded; --no-rename disables this

- scan progress with -r: "N eligible files found" for every 100 folders scanned,
  and a "N folders scanned" tally in the summary
- per-type file summary (console + optional log)
- live overall progress ("x of y files processed") and per-video percent
- adaptive CPU scheduler, image/ffmpeg job separation
- ffmpeg pipeline (output written to a .part file, then moved into place)
- global conflict-safe naming
- offers to install missing prerequisites (Pillow, pillow-heif, ffmpeg)
- --merge joins the videos of each folder into one MP4 (resized/padded to the largest)
- --cleanup delete|save removes sources that already have a converted file next to them
  (delete, or move to ./cleanup keeping the folder tree); no conversion is done.
  Clashing names (clip.wmv, clip.avi, clip.mp4, clip_001.mp4) are paired by size and
  duration (images: size and a small fingerprint); --verify does this for every file

Run with --help for the full switch list.
"""

import os
import re
import json
import sys
import glob
import shutil
import argparse
import importlib
import subprocess
import threading
import time
from pathlib import Path
from fractions import Fraction
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

VERSION = "5.7"
VERSION_DATE = "2026-11-05"
VERSION_TEXT = f"mediaconv.py v{VERSION} ({VERSION_DATE})"

Image = ImageOps = None     # Pillow, loaded on demand by ensure_prerequisites()


# =========================================================
# FORMAT TABLE
# mode -> (source suffixes, kind, help text)
# =========================================================

MODES = {
    # images -> .jpg (or .webp with --image-to webp)
    "jpeg": ((".jpeg",), "image", "rename .jpeg to .jpg"),
    "jpg":  ((".jpg", ".jpeg"), "image", "convert .jpg/.jpeg to WebP (needs --image-to webp)"),
    "png":  ((".png",), "image", "convert .png"),
    "webp": ((".webp",), "image", "convert .webp (to JPG)"),
    "bmp":  ((".bmp",), "image", "convert .bmp"),
    "tiff": ((".tif", ".tiff"), "image", "convert .tif/.tiff (first page)"),
    "ico":  ((".ico",), "image", "convert .ico (largest size)"),
    "heic": ((".heic", ".heif"), "image", "convert .heic/.heif (needs pillow-heif)"),
    "gifi": ((".gif",), "image", "extract the first frame of .gif (not in --images)"),
    # video -> .mp4
    "webm": ((".webm",), "video", "convert .webm to MP4"),
    "wmv":  ((".wmv",), "video", "convert .wmv to MP4"),
    "avi":  ((".avi",), "video", "convert .avi to MP4"),
    "flv":  ((".flv",), "video", "convert .flv to MP4"),
    "mov":  ((".mov",), "video", "convert .mov to MP4"),
    "mkv":  ((".mkv",), "video", "convert .mkv to MP4"),
    "mpg":  ((".mpg", ".mpeg"), "video", "convert .mpg/.mpeg to MP4"),
    "3gp":  ((".3gp",), "video", "convert .3gp to MP4"),
    "m4v":  ((".m4v",), "video", "convert .m4v to MP4"),
    "ts":   ((".ts", ".mts", ".m2ts"), "video", "convert .ts/.mts/.m2ts to MP4"),
    "gifv": ((".gif",), "video", "convert animated .gif to MP4 (not in --videos)"),
    "mp4":  ((".mp4",), "video", "source for --extract-audio or --merge only"),
    # audio -> .mp3 (or .m4a with --audio-to aac)
    "wma":  ((".wma",), "audio", "convert .wma"),
    "flac": ((".flac",), "audio", "convert .flac"),
    "wav":  ((".wav",), "audio", "convert .wav"),
    "ogg":  ((".ogg", ".oga"), "audio", "convert .ogg/.oga"),
    "m4a":  ((".m4a",), "audio", "convert .m4a (to MP3)"),
}

# Containers whose streams can usually be copied into MP4 without re-encoding.
REMUXABLE = {"mkv", "mov", "ts", "m4v", "3gp", "mpg"}

# Stream codecs that MP4 can carry; --remux copies them, anything else is re-encoded.
MP4_VIDEO = {"h264", "hevc", "mpeg4", "av1"}
MP4_AUDIO = {"aac", "mp3", "ac3", "eac3", "alac"}

# --images / --videos / --audio selectors (extras depend on options, see expand_modes)
IMAGE_GROUP = ["png", "webp", "bmp", "tiff", "ico", "heic"]
VIDEO_GROUP = ["webm", "wmv", "avi", "flv", "mov", "mkv", "mpg", "ts", "3gp", "m4v"]
AUDIO_GROUP = ["wma", "flac", "wav", "ogg", "m4a"]

HW_ENCODERS = {                     # (h264, h265)
    "nvenc": ("h264_nvenc", "hevc_nvenc"),
    "qsv": ("h264_qsv", "hevc_qsv"),
    "amf": ("h264_amf", "hevc_amf"),
}

# ffmpeg -progress reports elapsed output time in microseconds
OUT_TIME = re.compile(r"out_time_us=(\d+)")

LOG_FILE = "mediaconv.log"
ERR_FILE = "mediaconv.errors.log"
CLEANUP_DIR = "cleanup"             # --cleanup save target; never scanned (start folder only)
CLEANUP_WARN_FILE = "cleanup_warnings.log"

lock = threading.Lock()
results = Counter()
used_targets = set()
LOG_ENABLED = False


# =========================================================
# LOGGING
# =========================================================

def record(msg, err=False):
    """Errors go to stderr and always to the error log; other messages to the log only with --log.

    Both files are created on first write, so an error log exists only if an error occurred.
    """
    if err:
        progress.say(msg, file=sys.stderr)
    if err or LOG_ENABLED:
        with lock:
            with open(ERR_FILE if err else LOG_FILE, "a", encoding="utf-8") as f:
                f.write(msg + "\n")


# =========================================================
# PROGRESS
# =========================================================

class Progress:
    """Single live status line: overall count plus percent of active jobs.

    On a non-interactive stream the live line is replaced by one line per
    finished file, so redirected output stays readable.
    """

    def __init__(self):
        self.total = 0
        self.done = 0
        self.unit = "files"
        self.active = {}          # name -> (percent or None, note or None)
        self.live = sys.stdout.isatty()
        self._lock = threading.RLock()
        self._width = 0

    def _line(self):
        parts = [f"{self.done} of {self.total} {self.unit} processed"]
        for name, (pct, note) in self.active.items():
            if pct is not None or note:
                text = name[:30]
                if pct is not None:
                    text += f" {pct:3d}%"
                if note:
                    text += f" ({note})"
                parts.append(text)
        return " | ".join(parts)

    def _clear(self):
        if self.live and self._width:
            sys.stdout.write("\r" + " " * self._width + "\r")
            self._width = 0

    def _draw(self):
        if self.live and self.total:
            line = self._line()
            sys.stdout.write(line)
            sys.stdout.flush()
            self._width = len(line)

    def _redraw(self):
        if self.live:
            self._clear()
            self._draw()

    def say(self, msg, file=None):
        with self._lock:
            self._clear()
            print(msg, file=file or sys.stdout)
            self._draw()

    def update(self, name, pct, note=None):
        with self._lock:
            if self.active.get(name) != (pct, note):
                self.active[name] = (pct, note)
                self._redraw()

    def finish_file(self, name):
        with self._lock:
            self.active.pop(name, None)
            self.done += 1
            if self.live:
                self._redraw()
            else:
                print(f"[{self.done}/{self.total}] {name}")

    def close(self):
        with self._lock:
            self._clear()


progress = Progress()


# =========================================================
# PREREQUISITES (offer to install, never install silently)
# =========================================================

def confirm(question, args):
    if args.no_install:
        return False
    if args.yes:
        return True
    if not sys.stdin.isatty():
        print("(non-interactive session: re-run with --yes to allow installs)")
        return False
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def confirm_delete(question, args):
    """Ask before a destructive step; only --yes skips the prompt."""
    if args.yes:
        return True
    if not sys.stdin.isatty():
        print("(non-interactive session: re-run with --yes to allow deletion)")
        return False
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def import_module(name):
    importlib.invalidate_caches()
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def ensure_package(pip_name, module_name, why, args):
    mod = import_module(module_name)
    if mod:
        return mod

    cmd = [sys.executable, "-m", "pip", "install", pip_name]
    print(f"\n{pip_name} is required {why} but is not installed.")
    print("Install command: " + " ".join(cmd))
    if not confirm("Install it now?", args):
        return None

    if subprocess.run(cmd).returncode != 0:
        print(f"Installing {pip_name} failed (in a managed Python, try a virtual env).")
        return None
    return import_module(module_name)


def refresh_windows_path():
    """Pick up PATH changes made by an installer during this run."""
    try:
        import winreg
        extra = []
        for hive, sub in ((winreg.HKEY_CURRENT_USER, "Environment"),
                          (winreg.HKEY_LOCAL_MACHINE,
                           r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
            with winreg.OpenKey(hive, sub) as key:
                extra.append(os.path.expandvars(winreg.QueryValueEx(key, "Path")[0]))
        os.environ["PATH"] = os.pathsep.join([os.environ.get("PATH", "")] + extra)
    except (ImportError, OSError):
        pass
    local = os.environ.get("LOCALAPPDATA")
    if local:
        os.environ["PATH"] += os.pathsep + os.path.join(local, r"Microsoft\WinGet\Links")
        for d in glob.glob(os.path.join(local, r"Microsoft\WinGet\Packages\Gyan.FFmpeg*\*\bin")):
            os.environ["PATH"] += os.pathsep + d


def ffmpeg_install_command():
    if sys.platform == "win32" and shutil.which("winget"):
        return ["winget", "install", "--id", "Gyan.FFmpeg", "-e"]
    if sys.platform == "darwin" and shutil.which("brew"):
        return ["brew", "install", "ffmpeg"]
    if sys.platform.startswith("linux"):
        sudo = [] if hasattr(os, "geteuid") and os.geteuid() == 0 else ["sudo"]
        for tool, rest in (("apt-get", ["install", "-y", "ffmpeg"]),
                           ("dnf", ["install", "-y", "ffmpeg"]),
                           ("pacman", ["-S", "--noconfirm", "ffmpeg"]),
                           ("zypper", ["install", "-y", "ffmpeg"])):
            if shutil.which(tool):
                return sudo + [tool] + rest
    return None


def ensure_ffmpeg(args):
    if shutil.which("ffmpeg"):
        return True

    print("\nffmpeg is required for video/audio conversion but was not found on PATH.")
    cmd = ffmpeg_install_command()
    if cmd is None:
        print("No supported package manager found. Install ffmpeg from https://ffmpeg.org/")
        return False

    print("Install command: " + " ".join(cmd))
    if not confirm("Install ffmpeg now?", args):
        return False

    if subprocess.run(cmd).returncode != 0:
        print("ffmpeg installation failed.")
        return False

    if sys.platform == "win32":
        refresh_windows_path()
    if shutil.which("ffmpeg"):
        return True
    print("ffmpeg was installed but is not on PATH yet; open a new terminal and re-run.")
    return False


_encoders = None


def has_encoder(name):
    global _encoders
    if _encoders is None:
        try:
            _encoders = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                                       capture_output=True, text=True).stdout
        except OSError:
            _encoders = ""
    return re.search(rf"\b{re.escape(name)}\b", _encoders) is not None


def ensure_prerequisites(modes, args):
    """Return True when everything the selected modes need is available."""
    global Image, ImageOps

    kinds = {MODES[m][1] for m in modes}

    if "image" in kinds:
        pil = ensure_package("Pillow", "PIL", "for image conversion", args)
        if pil is None:
            return False
        from PIL import Image as _Image, ImageOps as _ImageOps
        Image, ImageOps = _Image, _ImageOps

    if "heic" in modes:
        heif = ensure_package("pillow-heif", "pillow_heif", "for HEIC/HEIF files", args)
        if heif is None:
            return False
        heif.register_heif_opener()

    if kinds & {"video", "audio"}:
        if not ensure_ffmpeg(args):
            return False
        if args.merge and not shutil.which("ffprobe"):
            print("ERROR: --merge needs ffprobe (it ships with ffmpeg) but it was not found.")
            return False
        if args.hw and not has_encoder(HW_ENCODERS[args.hw][1 if args.h265 else 0]):
            print(f"ERROR: ffmpeg has no {args.hw} encoder for this codec on this build.")
            return False

    return True


def check_prerequisites():
    def status(label, ok, note=""):
        print(f"  {label:<14}{'OK' if ok else 'MISSING'}{('  ' + note) if note else ''}")

    print("Prerequisites:")
    status("Pillow", import_module("PIL") is not None)
    status("pillow-heif", import_module("pillow_heif") is not None, "(only for --heic)")
    ffmpeg = shutil.which("ffmpeg")
    status("ffmpeg", bool(ffmpeg), "(video/audio)")
    status("ffprobe", bool(shutil.which("ffprobe")), "(video percent display)")
    if ffmpeg:
        for hw, names in HW_ENCODERS.items():
            status(f"hw {hw}", all(has_encoder(n) for n in names), "(--hw " + hw + ")")


# =========================================================
# CPU ADAPTIVE SCHEDULER
# =========================================================

def cpu_load():
    # os.getloadavg is unavailable on Windows; fall back to a neutral value.
    try:
        if hasattr(os, "getloadavg"):
            return min(1.0, os.getloadavg()[0] / (os.cpu_count() or 1))
    except OSError:
        pass
    return 0.5


BASE_IMG = max(2, (os.cpu_count() or 4) - 1)


def adaptive_workers():
    load = cpu_load()

    if load > 0.85:
        return 1, 1, load
    if load > 0.55:
        return max(2, BASE_IMG // 2), 1, load
    return BASE_IMG, 2, load


# =========================================================
# TARGETS AND UNIQUE FILE NAMES
# =========================================================

def audio_suffix(args):
    return ".mp3" if args.audio_to == "mp3" else ".m4a"


def target_suffix(mode, args):
    kind = MODES[mode][1]
    if kind == "image":
        return ".webp" if args.image_to == "webp" and mode != "jpeg" else ".jpg"
    if kind == "audio" or args.extract_audio:
        return audio_suffix(args)
    return ".mp4"


def unique(path: Path):
    """Reserve a target path that neither exists nor is claimed by another task."""
    with lock:
        if path not in used_targets and not path.exists():
            used_targets.add(path)
            return path

        i = 1
        while True:
            cand = path.with_name(f"{path.stem}_{i:03d}{path.suffix}")
            if cand not in used_targets and not cand.exists():
                used_targets.add(cand)
                return cand
            i += 1


def reserve(dst: Path, args):
    """Return the destination path under the conflict policy, or None to skip."""
    if args.overwrite:
        return dst
    if args.skip_existing and dst.exists():
        return None
    return unique(dst)


def resolve_target(src: Path, suffix: str, args):
    """Return the destination path, or None if the file should be skipped."""
    return reserve(src.with_suffix(suffix), args)


# =========================================================
# FILE COLLECTION
# =========================================================

SKIPPED_DIRS = {"$recycle.bin", "recycler", "system volume information", ".trashes"}


def skipped_dir(name, parent):
    """True for trash/system folders (silently never scanned) and for the
    --cleanup save folder in the start folder (saved files must not be converted again)."""
    low = name.lower()
    return (low in SKIPPED_DIRS or low.startswith(".trash")
            or (parent == "." and low == CLEANUP_DIR))


def collect(modes, recursive):
    """Single directory walk; suffix match is case-insensitive."""
    by_suffix = {}
    for m in modes:
        for s in MODES[m][0]:
            by_suffix.setdefault(s, []).append(m)
    tasks = []
    found = scanned = 0
    for root, dirs, names in os.walk("."):
        if not recursive:
            dirs.clear()
        else:
            dirs[:] = [d for d in dirs if not skipped_dir(d, root)]
        for name in names:
            f = Path(root, name)
            ms = by_suffix.get(f.suffix.lower())
            if ms:
                found += 1
                tasks.extend((f, m) for m in ms)
        scanned += 1
        if recursive and scanned % 100 == 0:
            progress.say(f"{found} eligible files found ({scanned} folders scanned)")
    return tasks, scanned


def print_file_summary(tasks, scanned):
    counts = Counter(mode for _, mode in tasks)

    print("\n---- Scan summary ----\n")

    msg = f"{scanned} folders scanned"
    print(msg)
    record(msg)

    for k in sorted(counts):
        msg = f"{counts[k]} {k.upper()} files"
        print(msg)
        record(msg)

    msg = f"{len(tasks)} total files"
    print(msg)
    record(msg)


# =========================================================
# CONVERTERS
# Each returns one of: ok, dryrun, skipped, error
# =========================================================

def delete_source(src):
    src.unlink()
    record(f"DELETE {src}")


def is_animated(src):
    """True for a multi-frame image; unreadable files count as animated."""
    try:
        from PIL import Image as PILImage
        with PILImage.open(src) as im:
            return bool(getattr(im, "is_animated", False))
    except Exception:
        return True


gifi_done = set()       # animated gifs whose first-frame image is written (under lock)


def finish_source(src, mode, args):
    """Delete the source after a successful conversion (-d).

    With --gifv and --gifi both selected, a gif feeds two jobs: it is kept
    until the first-frame image exists, then removed by the video job (which
    also runs, and just skips, for a static gif).
    """
    if not args.delete_source:
        return
    if mode == "gifi" and args.gifv:
        with lock:
            gifi_done.add(src)
        return
    if mode == "gifv" and args.gifi:
        with lock:
            image_ok = src in gifi_done
        if not image_ok:
            record(f"KEEP {src} (its first-frame image was not written)")
            return
    delete_source(src)


def begin(src, mode, args):
    """Common prologue: (dst, early_status). dst is None when finished early."""
    dst = resolve_target(src, target_suffix(mode, args), args)
    if dst is None:
        record(f"SKIP {src} (exists)")
        return None, "skipped"
    if args.dry_run:
        record(f"DRYRUN {mode} {src} -> {dst}")
        return None, "dryrun"
    return dst, None


def jpeg_rename(src, args, mode):
    dst, early = begin(src, mode, args)
    if early:
        return early

    try:
        os.replace(src, dst) if args.overwrite else src.rename(dst)
        record(f"RENAME {src} -> {dst}")
        return "ok"
    except Exception as e:
        record(f"ERROR rename {src}: {e}", True)
        return "error"


def image_convert(src, args, mode):
    dst, early = begin(src, mode, args)
    if early:
        return early

    to_webp = dst.suffix == ".webp"
    tmp = dst.with_name(dst.name + ".part")
    try:
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            exif = None if args.strip_metadata else im.getexif()

            if args.max_size:
                im.thumbnail((args.max_size, args.max_size), Image.LANCZOS)

            has_alpha = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
            if to_webp and has_alpha:
                im = im.convert("RGBA")
            elif has_alpha:
                # flatten transparency onto white instead of black
                rgba = im.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.getchannel("A"))
                im = bg
            else:
                im = im.convert("RGB")

            kwargs = {"quality": args.quality or (90 if to_webp else 95)}
            if exif is not None and len(exif):
                kwargs["exif"] = exif.tobytes()
            im.save(tmp, "WEBP" if to_webp else "JPEG", **kwargs)

        os.replace(tmp, dst)
        record(f"CONVERT {mode} {src} -> {dst}")

        finish_source(src, mode, args)
        return "ok"

    except Exception as e:
        tmp.unlink(missing_ok=True)
        record(f"ERROR {mode} {src}: {e}", True)
        return "error"


def cli_path(path):
    """Path as a command-line argument; a name starting with '-' gets a ./ prefix
    so ffmpeg/ffprobe do not read it as an option."""
    text = str(path)
    return os.path.join(".", text) if text.startswith("-") else text


def probe_duration(src):
    """Duration in seconds via ffprobe, or None if unavailable."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", cli_path(src)],
            capture_output=True, text=True, timeout=30).stdout.strip()
        return float(out) if out else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def probe_codecs(src):
    """(video codec, audio codec) of the first streams via ffprobe; None = absent/unknown."""
    found = {}
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
             "-of", "csv=p=0", cli_path(src)],
            capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None, None
    for line in out.splitlines():
        name, _, ctype = line.strip().partition(",")
        found.setdefault(ctype, name)
    return found.get("video"), found.get("audio")


def is_plain_mp4(src):
    """True if src is already a valid MP4 container with MP4-compatible streams,
    so a .m4v can simply be renamed to .mp4. Any doubt (unreadable, QuickTime
    brand, unknown codec such as DRM-protected video) answers False."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=format_name:format_tags=major_brand:stream=codec_type,codec_name",
             "-of", "json", cli_path(src)],
            capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return False
        info = json.loads(out.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False

    fmt = info.get("format", {})
    if "mp4" not in fmt.get("format_name", "").split(","):
        return False
    if fmt.get("tags", {}).get("major_brand", "").strip().lower() == "qt":
        return False
    video, audio = probe_codecs(src)
    return video in MP4_VIDEO and (audio is None or audio in MP4_AUDIO)


def rename_convert(src, dst, args):
    """Move src to dst (-d), or copy it when the source is to be kept.
    The copy goes through a .part file like every other output."""
    if args.delete_source:
        os.replace(src, dst) if args.overwrite else src.rename(dst)
        record(f"RENAME {src} -> {dst}")
        return
    tmp = dst.with_name(dst.name + ".part")
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    record(f"COPY {src} -> {dst}")


def remux_args(src):
    """ffmpeg args to copy streams into MP4, or None if the codecs need re-encoding."""
    video, audio = probe_codecs(src)
    if video not in MP4_VIDEO:
        return None
    return ["-sn", "-c:v", "copy", *(["-tag:v", "hvc1"] if video == "hevc" else []),
            "-c:a", "copy" if audio in MP4_AUDIO or audio is None else "aac",
            "-movflags", "+faststart"]


def run_ffmpeg(srcs, tmp, out_args, fmt, duration, label=None, segments=None):
    """Run ffmpeg over the input files into tmp, reporting percent.

    segments are the input durations of a merge; they let the status line say
    which input is being added. Raises RuntimeError on failure.
    """
    label = label or srcs[0].name
    cmd = ["ffmpeg", "-y", "-nostdin", "-loglevel", "error",
           "-progress", "pipe:1", "-nostats",
           *[a for src in srcs for a in ("-i", cli_path(src))],
           *out_args, "-f", fmt, cli_path(tmp)]

    def adding(seconds):
        """Note naming the merge input that output time `seconds` falls in."""
        if not segments:
            return None
        end = 0
        for k, length in enumerate(segments, 1):
            end += length
            if seconds < end:
                return f"adding file {k}/{len(segments)}"
        return f"adding file {len(segments)}/{len(segments)}"

    progress.update(label, 0 if duration else None, adding(0))

    # stderr is merged into stdout so one reader drains both (no deadlock)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            errors="replace")
    errors = []
    for line in proc.stdout:
        line = line.strip()
        m = OUT_TIME.match(line)
        if m:
            if duration:
                pct = min(99, int(int(m.group(1)) / 1e6 / duration * 100))
                progress.update(label, pct, adding(int(m.group(1)) / 1e6))
        elif line and "=" not in line:
            errors.append(line)
    proc.wait()

    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exit {proc.returncode}: {' '.join(errors)[-500:]}")


EVEN_DIMS = "scale=trunc(iw/2)*2:trunc(ih/2)*2"     # yuv420p needs even sizes


def video_encoder_args(args):
    """Video codec, quality and pixel format options shared by convert and merge."""
    if args.hw:
        enc = HW_ENCODERS[args.hw][1 if args.h265 else 0]
    else:
        enc = "libx265" if args.h265 else "libx264"

    q = str(args.crf if args.crf is not None else (28 if args.h265 else 23))
    if enc.startswith("lib"):
        quality = ["-crf", q, "-preset", args.preset]
    elif args.hw == "nvenc":
        quality = ["-rc", "vbr", "-cq", q]
    elif args.hw == "qsv":
        quality = ["-global_quality", q]
    else:   # amf
        quality = ["-rc", "cqp", "-qp_i", q, "-qp_p", q]

    return ["-c:v", enc, *quality, "-pix_fmt", "yuv420p",
            *(["-tag:v", "hvc1"] if args.h265 else [])]


def encode_args(mode, args):
    if mode == "gifv":
        return ["-an", "-vf", EVEN_DIMS, "-c:v", "libx264", "-crf", "23",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart"]

    return ["-sn", "-vf", EVEN_DIMS, *video_encoder_args(args),
            "-c:a", "aac", "-movflags", "+faststart"]


def video_convert(src, args, mode):
    shortcut = (mode == "m4v" and not args.no_rename and not args.extract_audio
                and is_plain_mp4(src))
    dst, early = begin(src, mode, args)
    if early:
        if early == "dryrun" and shortcut:
            record(f"DRYRUN m4v {src}: valid MP4, would rename instead of re-encoding")
        return early

    if shortcut:
        try:
            rename_convert(src, dst, args)
            return "ok"
        except Exception as e:
            record(f"WARN rename failed for {src}, re-encoding: {e}")

    tmp = dst.with_name(dst.name + ".part")
    attempts = []
    if args.remux and mode in REMUXABLE:
        copy_args = remux_args(src)
        if copy_args:
            attempts.append(("remux", copy_args))
    attempts.append(("encode", encode_args(mode, args)))

    try:
        duration = probe_duration(src)
        for i, (how, out_args) in enumerate(attempts):
            try:
                run_ffmpeg([src], tmp, out_args, "mp4", duration)
                break
            except RuntimeError as e:
                tmp.unlink(missing_ok=True)
                if i == len(attempts) - 1:
                    raise
                record(f"WARN {how} failed for {src}, re-encoding: {e}")
        else:
            how = "encode"

        os.replace(tmp, dst)
        record(f"CONVERT ({how}) {mode} {src} -> {dst}")

        finish_source(src, mode, args)
        return "ok"

    except Exception as e:
        tmp.unlink(missing_ok=True)
        record(f"ERROR {mode} {src}: {e}", True)
        return "error"


def audio_convert(src, args, mode):
    """Audio-to-audio conversion, or audio extraction from a video (--extract-audio)."""
    dst, early = begin(src, mode, args)
    if early:
        return early

    tmp = dst.with_name(dst.name + ".part")
    if args.audio_to == "mp3":
        codec, fmt = ["-c:a", "libmp3lame", "-id3v2_version", "3"], "mp3"
    else:
        codec, fmt = ["-c:a", "aac"], "ipod"
    out_args = ["-vn", "-map_metadata", "0", *codec, "-b:a", args.abitrate]

    try:
        run_ffmpeg([src], tmp, out_args, fmt, probe_duration(src))
        os.replace(tmp, dst)
        record(f"CONVERT {mode} {src} -> {dst}")

        if args.delete_source:
            delete_source(src)
        return "ok"

    except Exception as e:
        tmp.unlink(missing_ok=True)
        record(f"ERROR {mode} {src}: {e}", True)
        return "error"


# =========================================================
# MERGE (--merge): join the videos of one folder into one MP4
# =========================================================

MERGE_AUDIO_RATE = 48000
MERGED_NAME = re.compile(r"_merged(_\d{3})?$", re.IGNORECASE)   # outputs of an earlier run


def natural_key(path):
    """Case-insensitive, numeric-aware sort key: clip2 sorts before clip10."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", path.name)]


def build_merge_jobs(tasks):
    """Group (file, mode) tasks by folder: [(folder, [(file, mode), ...]), ...].

    Folders with fewer than two videos are reported and left out, as are
    outputs of an earlier merge (<folder>_merged.mp4).
    """
    groups = {}
    for src, mode in tasks:
        if not MERGED_NAME.search(src.stem):
            groups.setdefault(src.parent, []).append((src, mode))

    jobs = []
    for folder in sorted(groups, key=lambda f: [natural_key(Path(p)) for p in f.parts]):
        files = sorted(groups[folder], key=lambda t: natural_key(t[0]))
        if len(files) < 2:
            print(f"Skipping {folder}: only one video, nothing to merge")
            record(f"SKIP {folder} (only one video)")
        else:
            jobs.append((folder, files))
    return jobs


def probe_media(src):
    """Facts needed to merge one file, or None if ffprobe cannot read a video stream.

    Returns dict(width, height, fps, has_audio, duration); width/height are
    the displayed size (after rotation metadata), fps a Fraction.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,width,height,avg_frame_rate,r_frame_rate"
             ":stream_tags=rotate:stream_side_data=rotation:format=duration",
             "-of", "json", cli_path(src)],
            capture_output=True, text=True, timeout=60).stdout
        info = json.loads(out)
        streams = info.get("streams", [])
        video = next(s for s in streams
                     if s.get("codec_type") == "video" and s.get("width"))
    except (OSError, ValueError, StopIteration, subprocess.SubprocessError):
        return None

    width, height = int(video["width"]), int(video["height"])
    rotation = [d["rotation"] for d in video.get("side_data_list", []) if "rotation" in d]
    try:
        rotation.append(float(video.get("tags", {}).get("rotate", 0)))
    except ValueError:
        pass
    if any(round(abs(r)) % 180 == 90 for r in rotation):
        width, height = height, width

    fps = Fraction(30)
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            cand = Fraction(video.get(key, "0"))
        except (ValueError, ZeroDivisionError):
            continue
        if cand > 0:
            fps = cand
            break

    try:
        duration = float(info.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        duration = None

    return {"width": width, "height": height, "fps": fps, "duration": duration,
            "has_audio": any(s.get("codec_type") == "audio" for s in streams)}


def merge_args(infos, args):
    """ffmpeg output args that scale/pad every input to the largest frame and concatenate."""
    big = max(infos, key=lambda i: i["width"] * i["height"])
    w, h = big["width"] + big["width"] % 2, big["height"] + big["height"] % 2
    fps = max(i["fps"] for i in infos)

    chains, joined = [], ""
    for n, i in enumerate(infos):
        chains.append(
            f"[{n}:v:0]scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,"
            f"fps={fps.numerator}/{fps.denominator},format=yuv420p[v{n}]")
        if i["has_audio"]:
            chains.append(f"[{n}:a:0]aresample={MERGE_AUDIO_RATE},"
                          f"aformat=sample_fmts=fltp:channel_layouts=stereo[a{n}]")
        else:
            chains.append(f"anullsrc=r={MERGE_AUDIO_RATE}:cl=stereo,"
                          f"atrim=duration={i['duration']:.3f},asetpts=PTS-STARTPTS[a{n}]")
        joined += f"[v{n}][a{n}]"
    chains.append(f"{joined}concat=n={len(infos)}:v=1:a=1[v][a]")

    return ["-filter_complex", ";".join(chains), "-map", "[v]", "-map", "[a]",
            *video_encoder_args(args), "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart"]


def merge_folder(folder, files, args):
    """Merge the videos of one folder into <folder>_merged.mp4."""
    name = folder.resolve().name or "merged"
    dst = reserve(folder / f"{name}_merged.mp4", args)
    if dst is None:
        record(f"SKIP merge {folder} (exists)")
        return "skipped"

    srcs = [src for src, _ in files]
    progress.say(f"Merging {len(srcs)} videos in {folder} -> {dst.name}")
    if args.dry_run:
        record(f"DRYRUN merge {len(srcs)} files in {folder} -> {dst}: "
               + ", ".join(s.name for s in srcs))
        return "dryrun"

    tmp = dst.with_name(dst.name + ".part")
    try:
        infos, usable = [], []
        for src in srcs:
            info = probe_media(src)
            if info is None:
                record(f"WARN merge: {src} is not a readable video, left out")
            elif not info["has_audio"] and not info["duration"]:
                record(f"WARN merge: {src} has no audio and an unknown length, left out")
            else:
                infos.append(info)
                usable.append(src)
        if len(usable) < 2:
            raise RuntimeError("fewer than two readable videos")

        durations = [i["duration"] for i in infos]
        total = sum(durations) if all(durations) else None
        run_ffmpeg(usable, tmp, merge_args(infos, args), "mp4", total, label=dst.name,
                   segments=durations if total else None)

        if not tmp.exists() or tmp.stat().st_size == 0:
            raise RuntimeError("ffmpeg produced no output")
        os.replace(tmp, dst)
        record(f"MERGE {len(usable)} files in {folder} -> {dst}: "
               + ", ".join(s.name for s in usable))

        if args.delete_source:
            for src in usable:
                try:
                    delete_source(src)
                except OSError as e:
                    record(f"ERROR delete {src}: {e}", True)
        return "ok"

    except Exception as e:
        tmp.unlink(missing_ok=True)
        record(f"ERROR merge {folder}: {e}", True)
        return "error"


def convert(mode, src, args):
    kind = MODES[mode][1]
    if mode == "gifv" and args.gifi and not is_animated(src):
        record(f"SKIP {src} (static gif, --gifi writes the image)")
        if not args.dry_run:
            finish_source(src, mode, args)
        return "skipped"
    if mode == "jpeg":
        return jpeg_rename(src, args, mode)
    if kind == "image":
        return image_convert(src, args, mode)
    if kind == "audio" or args.extract_audio:
        return audio_convert(src, args, mode)
    return video_convert(src, args, mode)


# =========================================================
# WORKER AND PIPELINE
# =========================================================

def worker(task, args):
    src, mode = task

    # console-only progress line
    progress.say(f"Processing: {src.name}")

    status = "error"
    try:
        status = convert(mode, src, args)
    except Exception as e:
        record(f"ERROR {mode} {src}: {e}", True)
    finally:
        with lock:
            results[status] += 1
        progress.finish_file(src.name)
    return status


def run_pipeline(tasks, args):
    image_tasks = [t for t in tasks if MODES[t[1]][1] == "image"]
    ffmpeg_tasks = [t for t in tasks if MODES[t[1]][1] != "image"]

    img_workers, ff_workers, load = adaptive_workers()

    print(f"\nScheduler load={load:.2f}")
    print(f"Image workers={img_workers}")
    print(f"ffmpeg workers={ff_workers}\n")

    # Images and ffmpeg jobs run as separate groups so ffmpeg (already
    # multi-threaded) does not compete with the image workers.
    for batch, n in ((image_tasks, img_workers), (ffmpeg_tasks, ff_workers)):
        if batch:
            with ThreadPoolExecutor(max_workers=n) as ex:
                list(ex.map(lambda t: worker(t, args), batch))


def merge_worker(job, args):
    folder, files = job
    status = "error"
    try:
        status = merge_folder(folder, files, args)
    except Exception as e:
        record(f"ERROR merge {folder}: {e}", True)
    finally:
        with lock:
            results[status] += 1
        progress.finish_file(folder.resolve().name or str(folder))
    return status


def run_merge(jobs, args):
    _, ff_workers, load = adaptive_workers()
    progress.unit = "merge files"

    print(f"\nScheduler load={load:.2f}")
    print(f"ffmpeg workers={ff_workers}\n")

    with ThreadPoolExecutor(max_workers=ff_workers) as ex:
        list(ex.map(lambda j: merge_worker(j, args), jobs))


# =========================================================
# CLEANUP (--cleanup): remove sources whose converted file already exists
# =========================================================

def converted_exists(dst: Path, src: Path):
    """True if dst is a non-empty file and not the source itself."""
    try:
        return dst.is_file() and dst.stat().st_size > 0 and not os.path.samefile(src, dst)
    except OSError:
        return False


def missing_note(dst: Path):
    """Why a source is kept although a file of the converted name exists, or None."""
    try:
        if dst.is_file() and dst.stat().st_size == 0:
            return f"converted file {dst.name} is empty; source kept"
    except OSError:
        pass
    return None


CLEANUP_NNN = re.compile(r"_\d{3}$")
IMAGE_TOLERANCE = 16.0          # mean luma difference (0-255) of the 8x8 fingerprints
TIME_FLOOR, IMAGE_FLOOR = 0.05, 4.0     # extra margin the runner-up needs (s / luma)


def cleanup_key(stem):
    """Case-folded name without a trailing _NNN conflict suffix."""
    return (CLEANUP_NNN.sub("", stem) or stem).casefold()


def cleanup_kind(suffix):
    """What to compare for a converted file: audio (duration), video or image."""
    suffix = suffix.lower()
    return "audio" if suffix in (".mp3", ".m4a") else "video" if suffix == ".mp4" else "image"


def duration_of(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", cli_path(path)],
            capture_output=True, text=True, timeout=60).stdout
        return float(out.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def cleanup_facts(path, kind):
    """Metadata used to pair a source with its converted file, or None if unreadable.

    audio: duration. video: displayed size and duration. image: displayed size
    (EXIF orientation applied) and an 8x8 grayscale fingerprint.
    """
    if kind != "image":
        if not shutil.which("ffprobe"):
            return None
        if kind == "audio":
            d = duration_of(path)
            return None if d is None else {"duration": d}
        info = probe_media(path)
        if not info or info["duration"] is None:
            return None
        return {"size": (info["width"], info["height"]), "duration": info["duration"]}
    try:
        from PIL import Image, ImageOps
        with Image.open(path) as im:
            w, h = im.size
            if im.getexif().get(0x0112, 1) in (5, 6, 7, 8):
                w, h = h, w
            im.draft("L", (64, 64))
            im = ImageOps.exif_transpose(im)
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                im = Image.alpha_composite(Image.new("RGBA", im.size, "white"), im)
            small = im.convert("L").resize((8, 8), Image.BOX)
            # getdata() is deprecated in Pillow 12+; get_flattened_data() replaces it
            pixels = small.get_flattened_data() if hasattr(small, "get_flattened_data") \
                else small.getdata()
            return {"size": (w, h), "print": list(pixels)}
    except Exception:
        return None


def cleanup_cost(a, b):
    """Distance between two files' metadata, or None if they cannot be the same content."""
    if a.get("size") != b.get("size"):
        return None
    if "duration" in a:
        diff = abs(a["duration"] - b["duration"])
        return diff if diff <= max(0.5, 0.01 * max(a["duration"], b["duration"])) else None
    diff = sum(abs(x - y) for x, y in zip(a["print"], b["print"])) / 64
    return diff if diff <= IMAGE_TOLERANCE else None


def describe_facts(f):
    parts = []
    if f.get("size"):
        parts.append("%dx%d" % f["size"])
    if "duration" in f:
        parts.append(f"{f['duration']:.1f} s")
    return ", ".join(parts)


def pair_by_metadata(sources, cands, facts):
    """Pair sources with converted files whose metadata agrees.

    A pair is made only when it is the clear best choice for both sides (the
    runner-up must be clearly worse), so look-alikes are never guessed.
    Returns (pairs {source: converted}, why {source: reason it was not paired}).
    """
    cost = {}
    for s in sources:
        for c in cands:
            if facts.get(s) and facts.get(c):
                x = cleanup_cost(facts[s], facts[c])
                if x is not None:
                    cost[(s, c)] = x

    floor = TIME_FLOOR if any("duration" in f for f in facts.values() if f) else IMAGE_FLOOR

    def clear(ranked):
        return len(ranked) < 2 or ranked[1] > 2 * ranked[0] + floor

    pairs, why = {}, {}
    for s in sources:
        mine = sorted((cost[(s, c)], c) for c in cands if (s, c) in cost)
        if not facts.get(s):
            why[s] = ("metadata could not be read (unreadable file, or ffprobe/Pillow "
                      "missing); source kept")
        elif not mine:
            seen = ", ".join(f"{c.name} ({describe_facts(facts[c])})"
                             for c in cands if facts.get(c))
            why[s] = (f"no converted file matches its metadata ({describe_facts(facts[s])}); "
                      f"candidates: {seen or 'none readable'}; source kept")
        elif not clear([x for x, _ in mine]):
            why[s] = ("metadata cannot tell it apart from the converted files "
                      + ", ".join(c.name for _, c in mine) + "; source kept")
        else:
            c = mine[0][1]
            theirs = sorted(cost[(o, c)] for o in sources if (o, c) in cost)
            if theirs[0] == cost[(s, c)] and clear(theirs):
                pairs[s] = c
            else:
                others = [o.name for o in sources if o != s and (o, c) in cost]
                why[s] = f"{c.name} fits it as well as {', '.join(others)}; source kept"
    return pairs, why


def renamed_stem(src, converted):
    """Name stem for a saved source: that of its converted file, when they differ."""
    stems = {c.stem for c in converted}
    if len(stems) == 1:
        stem = stems.pop()
        if stem.casefold() != src.stem.casefold():
            return stem
    return src.stem


def plan_cleanup(tasks, args):
    """Split the collected sources into (removable, kept, ambiguous, notes).

    removable is a list of (source, converted files, name stem to save under).
    A source is removable when every converted file it needs was found.
    Normally that is the exact name (clip.wmv -> clip.mp4). When several
    sources or converted files share a name once a _NNN suffix is ignored
    (clip.wmv, clip.avi, clip.mp4, clip_001.mp4), or with --verify, they are
    paired by metadata instead (see cleanup_facts). Sources without a clear
    partner are ambiguous. notes is a list of (source, explanation) for the
    warning log.
    """
    wanted = {}                         # src -> converted paths
    for src, mode in tasks:
        wanted.setdefault(src, set()).add(src.with_suffix(target_suffix(mode, args)))

    buckets = {}                        # (folder, name, suffix) -> sources
    for src, dsts in wanted.items():
        for dst in dsts:
            key = (src.parent, cleanup_key(src.stem), dst.suffix.lower())
            buckets.setdefault(key, []).append(src)

    listings, jobs = {}, []
    got, nofile, unclear = {}, {}, {}   # got[src][suffix] = converted file
    for (folder, key, suf), srcs in sorted(buckets.items()):
        if folder not in listings:
            try:
                listings[folder] = os.listdir(folder)
            except OSError:
                listings[folder] = []
        own = {os.path.normcase(os.path.abspath(s)) for s in srcs}
        pat = re.compile(re.escape(key) + r"(_\d{3})?" + re.escape(suf), re.I)
        cands = []
        for name in listings[folder]:
            path = folder / name
            try:
                if (pat.fullmatch(name) and path.is_file() and path.stat().st_size > 0
                        and os.path.normcase(os.path.abspath(path)) not in own):
                    cands.append(path)
            except OSError:
                pass
        if not cands:
            for s in srcs:
                nofile.setdefault(s, []).append(missing_note(s.with_suffix(suf)))
        elif (len(srcs) == 1 and len(cands) == 1 and not args.verify
              and os.path.normcase(cands[0].name)
              == os.path.normcase(srcs[0].with_suffix(suf).name)):
            got.setdefault(srcs[0], {})[suf] = cands[0]
        else:
            jobs.append((srcs, cands, suf))

    need = sorted({(p, cleanup_kind(suf)) for srcs, cands, suf in jobs for p in srcs + cands},
                  key=str)
    if need:
        print(f"Comparing the metadata of {len(need)} files in {len(jobs)} groups ...")
        if any(kind == "image" for _, kind in need):
            try:
                importlib.import_module("pillow_heif").register_heif_opener()
            except (ImportError, AttributeError):
                pass
        with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 4)) as ex:
            facts = dict(zip(need, ex.map(lambda k: cleanup_facts(*k), need)))
        for srcs, cands, suf in jobs:
            kind = cleanup_kind(suf)
            pairs, why = pair_by_metadata(srcs, cands,
                                          {p: facts[(p, kind)] for p in srcs + cands})
            for s in srcs:
                if s in pairs:
                    got.setdefault(s, {})[suf] = pairs[s]
                    record(f"MATCH {s} <-> {pairs[s].name} ({describe_facts(facts[(s, kind)])})")
                else:
                    unclear.setdefault(s, []).append(why[s])

    removable, kept, ambiguous, notes = [], [], [], []
    for src in wanted:
        notes += [(src, n) for n in nofile.get(src, []) + unclear.get(src, []) if n]
        if src in nofile:
            kept.append(src)
        elif src in unclear:
            ambiguous.append(src)
        else:
            converted = [got[src][k] for k in sorted(got[src])]
            removable.append((src, converted, renamed_stem(src, converted)))
    return removable, kept, ambiguous, notes


def report_cleanup_warnings(notes, args):
    """Print each warning and append it, with the full path, to CLEANUP_WARN_FILE."""
    if not notes:
        return
    # absolute(), not resolve(): resolve() turns a mapped drive (X:\) into its UNC path
    lines = [f"{src.absolute()}: {text}" for src, text in notes]
    for line in lines:
        print(f"WARNING: {line}")
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with lock:
        with open(CLEANUP_WARN_FILE, "a", encoding="utf-8") as f:
            f.write(f"# {stamp} {VERSION_TEXT} --cleanup {args.cleanup}"
                    f"{' (dry run)' if args.dry_run else ''}\n")
            f.write("\n".join(lines) + "\n")


def cleanup_source(src, converted, stem, args):
    """Delete src, or move it below CLEANUP_DIR keeping its folder tree.

    The saved copy is named stem + the source's extension (the converted file's
    name when it was paired by metadata, e.g. clip.mpg -> clip_001.mpg).

    Returns ok, dryrun or error. A move is one os.replace (atomic on one
    volume); across volumes it fails and the source stays where it is.
    """
    save = args.cleanup == "save"
    try:
        dest = unique(Path(CLEANUP_DIR) / src.with_name(stem + src.suffix)) if save else None
        if args.dry_run:
            print(f"Would move: {src} -> {dest}" if save else f"Would delete: {src}")
            record(f"DRYRUN cleanup {args.cleanup} {src}")
            return "dryrun"
        if save:
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dest)
            record(f"CLEANUP MOVE {src} -> {dest} (converted: "
                   + ", ".join(p.name for p in converted) + ")")
            print(f"Moved: {src} -> {dest}")
        else:
            delete_source(src)
            print(f"Deleted: {src}")
        return "ok"
    except OSError as e:
        record(f"ERROR cleanup {src}: {e}", True)
        return "error"


def run_cleanup(tasks, args):
    removable, kept, ambiguous, notes = plan_cleanup(tasks, args)

    report_cleanup_warnings(notes, args)
    for src in kept:
        record(f"KEEP {src} (no converted file)")

    if removable and args.cleanup == "delete" and not args.dry_run:
        print(f"\nWARNING: --cleanup delete will delete {len(removable)} source files "
              "that have a converted file.")
        if not confirm_delete("Delete these source files?", args):
            print("Cancelled; nothing was deleted.")
            return 1
        print()

    done = Counter(cleanup_source(src, conv, stem, args) for src, conv, stem in removable)

    verb = "deleted" if args.cleanup == "delete" else "moved"
    if args.dry_run:
        verb = "would be " + verb
    msg = (f"Cleanup done. {done['ok'] + done['dryrun']} {verb}, "
           f"{len(kept)} kept (no converted file), "
           f"{len(ambiguous)} skipped (no clear match), {done['error']} errors.")
    print()
    print(msg)
    record(msg)
    if notes:
        print(f"{len(notes)} warnings, see {CLEANUP_WARN_FILE}")
    return 1 if done["error"] else 0


# =========================================================
# COMMAND LINE
# =========================================================

EPILOG = """\
examples:
  mediaconv.py -c --png --webp                    PNG/WebP -> JPG, this folder
  mediaconv.py -r --images --image-to webp        all images -> WebP, recursive
  mediaconv.py -r --videos --remux -d             videos -> MP4 (copy streams when possible)
  mediaconv.py -r --mkv --h265 --crf 26           MKV -> H.265 MP4
  mediaconv.py -c --mp4 --extract-audio           pull MP3 audio out of MP4 files
  mediaconv.py -r --videos --merge                join the videos of each folder into one MP4
  mediaconv.py -r --videos --cleanup save         move already converted sources to ./cleanup
  mediaconv.py -r --heic --max-size 2048 -n       preview HEIC -> resized JPG
"""


class ShortUsageParser(argparse.ArgumentParser):
    """On a malformed command line, print version, error and a short usage hint."""

    def error(self, message):
        self.exit(2, f"{VERSION_TEXT}\nusage: {self.usage}\n"
                     f"error: {message}\nUse -h for the full switch list.\n")


def build_parser():
    p = ShortUsageParser(
        prog="mediaconv.py", allow_abbrev=False, add_help=False, epilog=EPILOG,
        usage="mediaconv.py (-c | -r) [format switches] [options]",
        description=f"{VERSION_TEXT}\nAdaptive media converter for images, video and audio.\n"
                    "Single-letter switches work in either case (-h = -H).",
        formatter_class=argparse.RawDescriptionHelpFormatter)

    g = p.add_argument_group("scope (one required)")
    g.add_argument("-c", "-C", "--current", action="store_true", help="current directory only")
    g.add_argument("-r", "-R", "--recursive", action="store_true", help="include subdirectories")

    g = p.add_argument_group("format selectors")
    g.add_argument("--images", action="store_true", help="all image formats")
    g.add_argument("--videos", action="store_true",
                   help="all video formats (with --merge also .mp4)")
    g.add_argument("--audio", action="store_true", help="all audio formats")

    for title, kind in (("image formats", "image"), ("video formats", "video"),
                        ("audio formats", "audio")):
        g = p.add_argument_group(title)
        for mode, (_, k, text) in MODES.items():
            if k == kind:
                g.add_argument(f"--{mode}", dest=mode, action="store_true", help=text)

    g = p.add_argument_group("image options")
    g.add_argument("--image-to", choices=("jpg", "webp"), default="jpg",
                   help="image target format (default: jpg)")
    g.add_argument("--quality", type=int, metavar="N",
                   help="image quality 1-100 (default: 95 jpg, 90 webp)")
    g.add_argument("--max-size", type=int, metavar="PX",
                   help="shrink images so the longest side is at most PX")
    g.add_argument("--strip-metadata", action="store_true",
                   help="drop EXIF/GPS metadata (kept by default)")

    g = p.add_argument_group("video options")
    g.add_argument("--h265", action="store_true", help="encode H.265/HEVC instead of H.264")
    g.add_argument("--crf", type=int, metavar="N",
                   help="quality, lower is better (default: 23 H.264, 28 H.265)")
    g.add_argument("--preset", default="medium", metavar="NAME",
                   choices=("ultrafast", "superfast", "veryfast", "faster", "fast",
                            "medium", "slow", "slower", "veryslow"),
                   help="software encoder speed preset (default: medium)")
    g.add_argument("--hw", choices=sorted(HW_ENCODERS),
                   help="hardware encoder: nvenc, qsv or amf")
    g.add_argument("--remux", action="store_true",
                   help="copy streams without re-encoding for mkv/mov/ts/m4v/3gp/mpg "
                        "(falls back to encoding if copy fails)")

    g.add_argument("--no-rename", action="store_true",
                   help="always re-encode .m4v files; by default a .m4v that already is a "
                        "valid MP4 is renamed to .mp4 (copied, unless -d)")

    g = p.add_argument_group("merge")
    g.add_argument("-m", "-M", "--merge", action="store_true",
                   help="join the videos of each folder (natural name order) into "
                        "<folder>_merged.mp4; smaller videos are scaled to fit the largest "
                        "frame and padded with black, frame rates are matched to the highest")

    g = p.add_argument_group("cleanup")
    g.add_argument("--cleanup", choices=("delete", "save"),
                   help="convert nothing; remove the selected sources that already have a "
                        "converted file next to them (same name, target extension): "
                        f"delete them, or save = move them to ./{CLEANUP_DIR} keeping the "
                        "folder tree")
    g.add_argument("--verify", action="store_true",
                   help="with --cleanup, compare the metadata (size, duration) of every "
                        "source and its converted file, not only where names are ambiguous")

    g = p.add_argument_group("audio options")
    g.add_argument("--audio-to", choices=("mp3", "aac"), default="mp3",
                   help="audio target: mp3, or aac in .m4a (default: mp3)")
    g.add_argument("--abitrate", default="192k", metavar="RATE",
                   help="audio bitrate (default: 192k)")
    g.add_argument("--extract-audio", action="store_true",
                   help="video sources produce audio files instead of MP4")

    g = p.add_argument_group("behavior")
    g.add_argument("-o", "-O", "--overwrite", action="store_true", help="overwrite existing output")
    g.add_argument("-s", "-S", "--skip-existing", action="store_true",
                   help="skip when output already exists")
    g.add_argument("-d", "-D", "--delete-source", action="store_true",
                   help="delete the source after a successful conversion "
                        "(with --merge: after a successful merge, after a warning)")
    g.add_argument("-n", "-N", "--dry-run", action="store_true", help="simulate only")
    g.add_argument("-l", "-L", "--log", action="store_true",
                   help=f"write the activity log ({LOG_FILE}); "
                        f"errors always go to {ERR_FILE}")

    g = p.add_argument_group("prerequisites")
    g.add_argument("-y", "-Y", "--yes", action="store_true",
                   help="allow installing missing prerequisites without asking")
    g.add_argument("--no-install", action="store_true",
                   help="never offer to install prerequisites")
    g.add_argument("--check", action="store_true",
                   help="report prerequisite status and exit")

    g = p.add_argument_group("help")
    g.add_argument("-h", "-H", "--help", action="help", help="show this help")
    g.add_argument("-v", "-V", "--version", action="version", version=VERSION_TEXT,
                   help="show version and exit")
    return p


def expand_modes(args):
    """Turn flags into the ordered list of active modes, or raise ValueError."""
    modes = [m for m in MODES if getattr(args, m)]

    if args.images:
        modes += IMAGE_GROUP if args.image_to == "jpg" else \
            [m for m in IMAGE_GROUP if m != "webp"] + ["jpg"]
    if args.videos:
        modes += VIDEO_GROUP + (["mp4"] if args.extract_audio or args.merge else [])
    if args.audio:
        modes += [m for m in AUDIO_GROUP if not (m == "m4a" and args.audio_to == "aac")]
    modes = list(dict.fromkeys(modes))

    if "jpg" in modes and "jpeg" in modes:
        raise ValueError("--jpeg (rename) and --jpg (convert) both match .jpeg files")
    if "jpg" in modes and args.image_to != "webp":
        raise ValueError("--jpg needs --image-to webp (JPG to JPG is a no-op)")
    if "webp" in modes and args.image_to == "webp":
        raise ValueError("--webp as a source cannot be combined with --image-to webp")
    if "jpeg" in modes and args.image_to == "webp":
        raise ValueError("--jpeg only renames to .jpg; use --jpg with --image-to webp")
    if "m4a" in modes and args.audio_to == "aac":
        raise ValueError("--m4a as a source cannot be combined with --audio-to aac")
    if "mp4" in modes and not (args.extract_audio or args.merge):
        raise ValueError("--mp4 is only a source for --extract-audio or --merge")
    if args.merge:
        if args.extract_audio:
            raise ValueError("--merge cannot be combined with --extract-audio")
        if args.remux:
            raise ValueError("--merge always re-encodes; --remux does not apply")
        if not any(MODES[m][1] == "video" for m in modes) or \
                any(MODES[m][1] != "video" for m in modes):
            raise ValueError("--merge works on video formats only (e.g. --videos, --mkv)")
    if args.verify and not args.cleanup:
        raise ValueError("--verify only applies to --cleanup")
    if args.cleanup:
        if args.merge:
            raise ValueError("--cleanup cannot be combined with --merge")
        if args.delete_source:
            raise ValueError("--cleanup cannot be combined with -d (it removes sources itself)")
        if args.overwrite or args.skip_existing:
            raise ValueError("--cleanup converts nothing; -o and -s do not apply")
    if args.extract_audio and not any(MODES[m][1] == "video" for m in modes):
        raise ValueError("--extract-audio needs a video selector (e.g. --mp4, --mkv, --videos)")
    if args.overwrite and args.skip_existing:
        raise ValueError("--overwrite and --skip-existing cannot be combined")
    if args.quality is not None and not 1 <= args.quality <= 100:
        raise ValueError("--quality must be 1-100")
    return modes


def main():
    global LOG_ENABLED

    parser = build_parser()
    args = parser.parse_args()
    LOG_ENABLED = args.log

    if args.check:
        check_prerequisites()
        return 0

    if not args.current and not args.recursive:
        parser.error("use --current (-c) or --recursive (-r)")

    try:
        modes = expand_modes(args)
    except ValueError as e:
        parser.error(str(e))

    if not modes:
        parser.error("select at least one format (e.g. --png, --images; see --help)")

    tasks, scanned = collect(modes, args.recursive)

    if not tasks:
        print("No matching files found.")
        return 0

    if args.cleanup:
        print_file_summary(tasks, scanned)
        print()
        return run_cleanup(tasks, args)

    jobs = None
    if args.merge:
        jobs = build_merge_jobs(tasks)
        if not jobs:
            print("No folder with at least two videos found.")
            return 0
        tasks = [t for _, files in jobs for t in files]

        if args.delete_source:
            print(f"\nWARNING: --merge with -d deletes up to {len(tasks)} source videos "
                  "after their folder has been merged successfully.")
            if not args.dry_run and not confirm_delete("Delete the merged source files?", args):
                print("Cancelled; nothing was merged or deleted.")
                return 1

    if not args.dry_run:
        needed = {m for _, m in tasks}
        if not ensure_prerequisites(needed, args):
            print("Missing prerequisites; nothing was converted (see --check, --yes).")
            return 1

    print_file_summary(tasks, scanned)
    if jobs is not None:
        print(f"{len(jobs)} merge jobs")
        progress.total = len(jobs)
        run_merge(jobs, args)
    else:
        progress.total = len(tasks)
        run_pipeline(tasks, args)
    progress.close()

    summary = ", ".join(f"{v} {k}" for k, v in sorted(results.items()))
    unit = "merge jobs" if jobs is not None else "files"
    print(f"\nDone. Processed {progress.done} {unit} ({summary}).")
    record(f"Done. {summary}")

    return 1 if results["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
