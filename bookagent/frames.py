"""Capture original video visuals in one FFmpeg pass and keep distinct slides."""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import uuid
from dataclasses import asdict
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter, ImageStat

from .models import Frame


_PTS = re.compile(r"\bpts_time:([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?)", re.IGNORECASE)
_CACHE_VERSION = 3


def _thumbnail(path: str) -> Image.Image:
    with Image.open(path) as source:
        return source.convert("RGB").resize((256, 144), Image.Resampling.LANCZOS)


def _difference(first: Image.Image, second: Image.Image) -> float:
    means = ImageStat.Stat(ImageChops.difference(first, second)).mean
    pixel_change = sum(means) / (len(means) * 255.0)
    # Text changes occupy little area on a white slide. Normalize edge changes
    # by the slide's existing text/detail instead of the entire white canvas.
    # Smoothing and a minimum denominator suppress minor compression noise.
    edges = [
        image.convert("L").filter(ImageFilter.GaussianBlur(0.8))
        .filter(ImageFilter.FIND_EDGES).crop((2, 2, image.width - 2, image.height - 2))
        for image in (first, second)
    ]
    detail = max(8.0, *(ImageStat.Stat(edge).mean[0] for edge in edges))
    edge_change = ImageStat.Stat(ImageChops.difference(*edges)).mean[0] / detail
    return max(pixel_change, min(1.0, edge_change))


def _select_frames(samples: list[Frame], threshold: float) -> list[Frame]:
    if not samples:
        return []
    first_image = _thumbnail(samples[0].path)
    selected = [samples[0]]
    anchor = first_image
    previous = first_image
    for sample in samples[1:]:
        current = _thumbnail(sample.path)
        if _difference(previous, current) > threshold or _difference(anchor, current) > threshold:
            selected.append(sample)
            anchor = current
        previous = current
    # The earliest sampled frame preserves scene onset for transcript alignment.
    # Replacing it with a later sharp frame would move the slide's apparent start.
    return selected


def _validate_options(interval: float, crop: tuple[float, float, float, float] | None, threshold: float) -> None:
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("Frame sampling interval must be finite and positive.")
    if not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("Slide difference threshold must be between 0 and 1.")
    if crop is not None:
        if len(crop) != 4 or not all(math.isfinite(value) for value in crop):
            raise ValueError("Crop must have four finite normalized values: x,y,width,height.")
        x, y, width, height = crop
        if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 + 1e-9 or y + height > 1 + 1e-9:
            raise ValueError("Normalized crop must have positive width/height and fit inside the video frame.")


def extract_frames(
    video: Path,
    outdir: Path,
    interval: float = 12.0,
    crop: tuple[float, float, float, float] | None = None,
    threshold: float = 0.12,
) -> list[Frame]:
    """Sample once, record source frame timestamps, and cache slide representatives.

    Sampling can miss a slide displayed for less than ``interval`` seconds.
    The crop is a normalized (x, y, width, height) region of the source frame.
    """
    video, outdir = Path(video).resolve(), Path(outdir).resolve()
    _validate_options(interval, crop, threshold)
    if not video.is_file():
        raise FileNotFoundError(f"Video does not exist: {video}")
    stat = video.stat()
    fingerprint = {
        "version": _CACHE_VERSION, "video": str(video), "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns, "interval": interval,
        "crop": list(crop) if crop is not None else None, "threshold": threshold,
    }
    cache_key = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()[:20]
    outdir.mkdir(parents=True, exist_ok=True)
    manifest = outdir / f"frames-{cache_key}.json"
    if manifest.is_file():
        try:
            saved = json.loads(manifest.read_text(encoding="utf-8"))
            frames = [Frame(**item) for item in saved["frames"]]
            if saved.get("fingerprint") == fingerprint and frames and all(Path(frame.path).is_file() for frame in frames):
                return frames
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass
    sample_dir = outdir / f"samples-{cache_key}"
    sample_dir.mkdir(exist_ok=True)
    for stale in sample_dir.glob("frame-*.jpg"):
        stale.unlink()
    filters = [
        "setpts=PTS-STARTPTS",
        f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,{interval:.10g})'",
    ]
    if crop is not None:
        x, y, width, height = crop
        filters.append(f"crop=iw*{width:.10g}:ih*{height:.10g}:iw*{x:.10g}:ih*{y:.10g}")
    filters.append("showinfo")
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "info", "-nostdin", "-y", "-i", str(video),
        "-map", "0:v:0", "-an", "-vf", ",".join(filters), "-fps_mode", "vfr", "-q:v", "2",
        str(sample_dir / "frame-%06d.jpg"),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is required to capture source slides. Install FFmpeg and put ffmpeg on PATH.") from exc
    if completed.returncode:
        raise RuntimeError(f"ffmpeg could not capture slides from {video.name}: {completed.stderr[-1000:].strip()}")
    times = [float(match.group(1)) for match in _PTS.finditer(completed.stderr)]
    paths = sorted(sample_dir.glob("frame-*.jpg"))
    if not paths or len(paths) != len(times):
        raise RuntimeError("FFmpeg frame output did not match its source timestamps; refusing to invent slide timestamps.")
    if not all(math.isfinite(value) and value >= 0 for value in times):
        raise RuntimeError("FFmpeg reported invalid frame timestamps.")
    samples = [Frame(timestamp=timestamp, path=str(path)) for timestamp, path in zip(times, paths)]
    frames = _select_frames(samples, threshold)
    temporary = manifest.with_name(f"{manifest.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps({"fingerprint": fingerprint, "frames": [asdict(frame) for frame in frames]}, indent=2), encoding="utf-8")
    temporary.replace(manifest)
    return frames
