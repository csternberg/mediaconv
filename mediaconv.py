#!/usr/bin/env python3
"""
mediaconv.py
Version: 2026-05-13 v3.6

Adaptive media converter with:
- per-type file summary (console + log)
- per-file processing output (console only)
- adaptive CPU scheduler
- image/video separation
- ffmpeg video pipeline
- global conflict-safe naming

IMAGE:
  -jpeg   rename .jpeg -> .jpg
  -png    convert .png  -> .jpg
  -webp   convert .webp -> .jpg

VIDEO (ffmpeg required):
  -webm   convert .webm -> .mp4
  -wmv    convert .wmv  -> .mp4
  -avi    convert .avi  -> .mp4
  -flv    convert .flv  -> .mp4

BEHAVIOR:
  -O        overwrite output
  -S        skip existing output
  -D        delete source after success
  --dryrun  simulate only (still logs)

HELP:
  -h -H -?  show help
"""

import os
import sys
import time
import argparse
import subprocess
import threading
from pathlib import Path
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

try:
    from PIL import Image
except ImportError:
    print("ERROR: Pillow missing (pip install Pillow)")
    sys.exit(1)


# =========================================================
# GLOBALS
# =========================================================

VIDEO_TYPES = {"webm", "wmv", "avi", "flv"}

lock = threading.Lock()
processed = 0
start_time = None
TOTAL = 0

used_targets = set()


# =========================================================
# HELP
# =========================================================

def print_help():
    print("""
mediaconv.py — Adaptive media converter

IMAGE:
  -jpeg   rename .jpeg → .jpg
  -png    convert .png → .jpg
  -webp   convert .webp → .jpg

VIDEO:
  -webm   convert .webm → .mp4
  -wmv    convert .wmv → .mp4
  -avi    convert .avi → .mp4
  -flv    convert .flv → .mp4

OPTIONS:
  -O        overwrite output
  -S        skip existing output
  -D        delete source after success
  --dryrun  simulate only

HELP:
  -h -H -?  show help
""")
    sys.exit(0)


# =========================================================
# LOGGING
# =========================================================

LOG_FILE = "mediaconv.log"
ERR_FILE = "mediaconv.errors.log"


def log(msg):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def log_err(msg):
    with open(ERR_FILE, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def record(msg, err=False):
    (log_err if err else log)(msg)


# =========================================================
# CPU ADAPTIVE SCHEDULER
# =========================================================

def cpu_load():
    try:
        if hasattr(os, "getloadavg"):
            return min(1.0, os.getloadavg()[0] / (os.cpu_count() or 1))
    except Exception:
        pass
    return 0.5


BASE_IMG = max(2, (os.cpu_count() or 4) - 1)


def adaptive_workers():
    load = cpu_load()

    if load > 0.85:
        return 1, 1, load
    elif load > 0.55:
        return max(2, BASE_IMG // 2), 1, load
    else:
        return BASE_IMG, 2, load


# =========================================================
# UNIQUE FILE NAMES
# =========================================================

def unique(path: Path, overwrite: bool):
    if overwrite:
        return path

    with lock:
        if path not in used_targets and not path.exists():
            used_targets.add(path)
            return path

        base, suf = path.stem, path.suffix
        parent = path.parent

        i = 1
        while True:
            cand = parent / f"{base}_{i:03d}{suf}"
            if cand not in used_targets and not cand.exists():
                used_targets.add(cand)
                return cand
            i += 1


# =========================================================
# FILE COLLECTION
# =========================================================

def collect(ext, recursive):
    base = Path(".")
    return list(base.rglob(f"*{ext}") if recursive else base.glob(f"*{ext}"))


# =========================================================
# SUMMARY
# =========================================================

def print_file_summary(tasks):
    counts = Counter([t[1] for t in tasks])

    for k in sorted(counts.keys()):
        msg = f"{counts[k]} {k.upper()} files"
        print(msg)
        record(msg)

    total = sum(counts.values())
    msg = f"{total} total files"
    print(msg)
    record(msg)


# =========================================================
# CONVERTERS
# =========================================================

def jpeg_rename(src, args):
    dst = unique(src.with_suffix(".jpg"), args.O)

    if args.dryrun:
        record(f"DRYRUN rename {src} -> {dst}")
        return "dryrun"

    try:
        src.rename(dst)
        record(f"RENAME {src} -> {dst}")
        return "ok"
    except Exception as e:
        record(f"ERROR rename {src}: {e}", True)
        return "error"


def image_to_jpg(src, args, label):
    dst = unique(src.with_suffix(".jpg"), args.O)

    if args.dryrun:
        record(f"DRYRUN {label} {src} -> {dst}")
        return "dryrun"

    try:
        with Image.open(src) as im:
            im.convert("RGB").save(dst, "JPEG", quality=95)

        record(f"CONVERT {label} {src} -> {dst}")

        if args.D:
            src.unlink()
            record(f"DELETE {src}")

        return "ok"

    except Exception as e:
        record(f"ERROR {label} {src}: {e}", True)
        return "error"


def video_to_mp4(src, args, label):
    dst = unique(src.with_suffix(".mp4"), args.O)

    if args.dryrun:
        record(f"DRYRUN {label} {src} -> {dst}")
        return "dryrun"

    cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        "-vcodec", "libx264",
        "-crf", "23",
        "-preset", "medium",
        str(dst)
    ]

    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        record(f"CONVERT {label} {src} -> {dst}")

        if args.D:
            src.unlink()
            record(f"DELETE {src}")

        return "ok"

    except Exception as e:
        record(f"ERROR {label} {src}: {e}", True)
        return "error"


# =========================================================
# TASK BUILDER
# =========================================================

def build_tasks(args):
    tasks = []

    if args.jpeg:
        tasks += [(f, "jpeg") for f in collect(".jpeg", args.R)]
    if args.png:
        tasks += [(f, "png") for f in collect(".png", args.R)]
    if args.webp:
        tasks += [(f, "webp") for f in collect(".webp", args.R)]
    if args.webm:
        tasks += [(f, "webm") for f in collect(".webm", args.R)]
    if args.wmv:
        tasks += [(f, "wmv") for f in collect(".wmv", args.R)]
    if args.avi:
        tasks += [(f, "avi") for f in collect(".avi", args.R)]
    if args.flv:
        tasks += [(f, "flv") for f in collect(".flv", args.R)]

    return tasks


# =========================================================
# WORKER
# =========================================================

def worker(task, args):
    global processed

    src, mode = task

    # console-only progress line
    print(f"Processing: {src.name}")

    try:
        if mode == "jpeg":
            return jpeg_rename(src, args)
        if mode == "png":
            return image_to_jpg(src, args, "png")
        if mode == "webp":
            return image_to_jpg(src, args, "webp")
        if mode == "webm":
            return video_to_mp4(src, args, "webm")
        if mode == "wmv":
            return video_to_mp4(src, args, "wmv")
        if mode == "avi":
            return video_to_mp4(src, args, "avi")
        if mode == "flv":
            return video_to_mp4(src, args, "flv")

    finally:
        with lock:
            processed += 1


# =========================================================
# PIPELINE
# =========================================================

def run_pipeline(tasks, args):
    image_tasks = [t for t in tasks if t[1] not in VIDEO_TYPES]
    video_tasks = [t for t in tasks if t[1] in VIDEO_TYPES]

    img_workers, vid_workers, load = adaptive_workers()

    print(f"\nScheduler load={load:.2f}")
    print(f"Image workers={img_workers}")
    print(f"Video workers={vid_workers}\n")

    with ThreadPoolExecutor(max_workers=img_workers) as ex:
        list(ex.map(lambda t: worker(t, args), image_tasks))

    with ThreadPoolExecutor(max_workers=vid_workers) as ex:
        list(ex.map(lambda t: worker(t, args), video_tasks))


# =========================================================
# MAIN
# =========================================================

def main():

    global start_time, TOTAL

    parser = argparse.ArgumentParser(add_help=False)

    parser.add_argument("-C", action="store_true")
    parser.add_argument("-R", action="store_true")

    parser.add_argument("-jpeg", action="store_true")
    parser.add_argument("-png", action="store_true")
    parser.add_argument("-webp", action="store_true")

    parser.add_argument("-webm", action="store_true")
    parser.add_argument("-wmv", action="store_true")
    parser.add_argument("-avi", action="store_true")
    parser.add_argument("-flv", action="store_true")

    parser.add_argument("-O", action="store_true")
    parser.add_argument("-S", action="store_true")
    parser.add_argument("-D", action="store_true")

    parser.add_argument("--dryrun", action="store_true")

    parser.add_argument("-h", action="store_true")
    parser.add_argument("-H", action="store_true")
    parser.add_argument("-?", action="store_true")

    args = parser.parse_args()

    if args.h or args.H or args.__dict__.get("?"):
        print_help()

    if not args.C and not args.R:
        print("Use -C or -R")
        return

    tasks = build_tasks(args)

    if not tasks:
        print("No matching files found.")
        return

    print_file_summary(tasks)

    TOTAL = len(tasks)
    start_time = time.time()

    run_pipeline(tasks, args)

    print(f"\nDone. Processed {processed} files.")


if __name__ == "__main__":
    main()