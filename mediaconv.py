#!/usr/bin/env python3
"""
mediaconv.py
Version: 2026-10-02 v5.1

Adaptive media converter (images, video, audio).

- per-type file summary (console + optional log)
- live overall progress ("x of y files processed") and per-video percent
- adaptive CPU scheduler, image/ffmpeg job separation
- ffmpeg pipeline (output written to a .part file, then moved into place)
- global conflict-safe naming
- offers to install missing prerequisites (Pillow, pillow-heif, ffmpeg)

Run with --help for the full switch list.
"""

import os
import re
import sys
import glob
import shutil
import argparse
import importlib
import subprocess
import threading
from pathlib import Path
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

VERSION = "5.1"
VERSION_DATE = "2026-10-02"
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
    "ts":   ((".ts", ".mts", ".m2ts"), "video",
             "convert .ts/.mts/.m2ts to MP4 (explicit only: .ts is also TypeScript)"),
    "gif":  ((".gif",), "video", "convert animated .gif to MP4"),
    "mp4":  ((".mp4",), "video", "source for --extract-audio only"),
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
VIDEO_GROUP = ["webm", "wmv", "avi", "flv", "mov", "mkv", "mpg", "3gp", "m4v", "gif"]
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
        self.active = {}          # name -> percent (None = unknown)
        self.live = sys.stdout.isatty()
        self._lock = threading.RLock()
        self._width = 0

    def _line(self):
        parts = [f"{self.done} of {self.total} files processed"]
        for name, pct in self.active.items():
            if pct is not None:
                parts.append(f"{name[:30]} {pct:3d}%")
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

    def update(self, name, pct):
        with self._lock:
            if self.active.get(name) != pct:
                self.active[name] = pct
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


def resolve_target(src: Path, suffix: str, args):
    """Return the destination path, or None if the file should be skipped."""
    dst = src.with_suffix(suffix)

    if args.overwrite:
        return dst
    if args.skip_existing and dst.exists():
        return None
    return unique(dst)


# =========================================================
# FILE COLLECTION
# =========================================================

def collect(modes, recursive):
    """Single directory walk; suffix match is case-insensitive."""
    by_suffix = {s: m for m in modes for s in MODES[m][0]}
    base = Path(".")
    entries = base.rglob("*") if recursive else base.glob("*")
    return [(f, by_suffix[f.suffix.lower()])
            for f in entries
            if f.suffix.lower() in by_suffix and f.is_file()]


def print_file_summary(tasks):
    counts = Counter(mode for _, mode in tasks)

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

        if args.delete_source:
            delete_source(src)
        return "ok"

    except Exception as e:
        tmp.unlink(missing_ok=True)
        record(f"ERROR {mode} {src}: {e}", True)
        return "error"


def probe_duration(src):
    """Duration in seconds via ffprobe, or None if unavailable."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(src)],
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
             "-of", "csv=p=0", str(src)],
            capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None, None
    for line in out.splitlines():
        name, _, ctype = line.strip().partition(",")
        found.setdefault(ctype, name)
    return found.get("video"), found.get("audio")


def remux_args(src):
    """ffmpeg args to copy streams into MP4, or None if the codecs need re-encoding."""
    video, audio = probe_codecs(src)
    if video not in MP4_VIDEO:
        return None
    return ["-sn", "-c:v", "copy", *(["-tag:v", "hvc1"] if video == "hevc" else []),
            "-c:a", "copy" if audio in MP4_AUDIO or audio is None else "aac",
            "-movflags", "+faststart"]


def run_ffmpeg(src, tmp, out_args, fmt, duration):
    """Run ffmpeg into tmp, reporting percent. Raises RuntimeError on failure."""
    cmd = ["ffmpeg", "-y", "-nostdin", "-loglevel", "error",
           "-progress", "pipe:1", "-nostats",
           "-i", str(src), *out_args, "-f", fmt, str(tmp)]

    progress.update(src.name, 0 if duration else None)

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
                progress.update(src.name, pct)
        elif line and "=" not in line:
            errors.append(line)
    proc.wait()

    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exit {proc.returncode}: {' '.join(errors)[-500:]}")


EVEN_DIMS = "scale=trunc(iw/2)*2:trunc(ih/2)*2"     # yuv420p needs even sizes


def encode_args(mode, args):
    if mode == "gif":
        return ["-an", "-vf", EVEN_DIMS, "-c:v", "libx264", "-crf", "23",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart"]

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

    return ["-sn", "-vf", EVEN_DIMS, "-c:v", enc, *quality, "-pix_fmt", "yuv420p",
            *(["-tag:v", "hvc1"] if args.h265 else []),
            "-c:a", "aac", "-movflags", "+faststart"]


def video_convert(src, args, mode):
    dst, early = begin(src, mode, args)
    if early:
        return early

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
                run_ffmpeg(src, tmp, out_args, "mp4", duration)
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

        if args.delete_source:
            delete_source(src)
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
        run_ffmpeg(src, tmp, out_args, fmt, probe_duration(src))
        os.replace(tmp, dst)
        record(f"CONVERT {mode} {src} -> {dst}")

        if args.delete_source:
            delete_source(src)
        return "ok"

    except Exception as e:
        tmp.unlink(missing_ok=True)
        record(f"ERROR {mode} {src}: {e}", True)
        return "error"


def convert(mode, src, args):
    kind = MODES[mode][1]
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
    g.add_argument("--videos", action="store_true", help="all video formats (not --ts)")
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
                   help="delete the source after a successful conversion")
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
        modes += VIDEO_GROUP + (["mp4"] if args.extract_audio else [])
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
    if "mp4" in modes and not args.extract_audio:
        raise ValueError("--mp4 is only a source for --extract-audio")
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

    tasks = collect(modes, args.recursive)

    if not tasks:
        print("No matching files found.")
        return 0

    if not args.dry_run:
        needed = {m for _, m in tasks}
        if not ensure_prerequisites(needed, args):
            print("Missing prerequisites; nothing was converted (see --check, --yes).")
            return 1

    print_file_summary(tasks)
    progress.total = len(tasks)
    run_pipeline(tasks, args)
    progress.close()

    summary = ", ".join(f"{v} {k}" for k, v in sorted(results.items()))
    print(f"\nDone. Processed {progress.done} files ({summary}).")
    record(f"Done. {summary}")

    return 1 if results["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
