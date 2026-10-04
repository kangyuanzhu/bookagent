"""Read timestamped captions and fetch a single video's available source files."""

from __future__ import annotations

import html
import hashlib
import json
import math
import re
import subprocess
from pathlib import Path
from typing import Any

from .models import Cue


_TIMING = re.compile(
    r"(?P<start>(?:\d+:)?\d{2}:\d{2}[.,]\d{1,3})\s*-->\s*"
    r"(?P<end>(?:\d+:)?\d{2}:\d{2}[.,]\d{1,3})"
)
_TAG = re.compile(r"<[^>]*>")
_INLINE_TIMESTAMP = re.compile(r"<(?:\d+:)?\d{2}:\d{2}[.,]\d{1,3}>")


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Transcript text must be a string.")
    return " ".join(html.unescape(_TAG.sub("", value)).split())


def _seconds(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    if len(parts) == 2:
        minutes, seconds = parts
        hours = "0"
    else:
        hours, minutes, seconds = parts
    if int(minutes) >= 60 or float(seconds) >= 60:
        raise ValueError(f"Invalid subtitle timestamp: {value}")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _cue(start: Any, end: Any, text: Any, where: str) -> Cue:
    try:
        if isinstance(start, bool) or isinstance(end, bool):
            raise ValueError
        start, end = float(start), float(end)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{where}: start and end must be numbers.") from exc
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
        raise ValueError(f"{where}: timestamps must be finite, start >= 0, and end > start.")
    text = _clean_text(text)
    if not text:
        raise ValueError(f"{where}: transcript text is empty.")
    return Cue(start=start, end=end, text=text)


def _word_key(word: str) -> str:
    return re.sub(r"^\W+|\W+$", "", word).casefold()


def _deduplicate_rolling(cues: list[Cue]) -> list[Cue]:
    """Remove repeated context in overlapping/adjacent rolling caption windows.

    Compare with the preceding *original* window, so A, A+B, B+C yields
    A, B, C. A one-word prefix is kept unless the whole cue is a repeat;
    that conservatively preserves intentional adjacent repeated words.
    """
    result: list[Cue] = []
    previous: Cue | None = None
    for cue in sorted(cues, key=lambda item: (item.start, item.end)):
        words = cue.text.split()
        overlap = 0
        if previous is not None and cue.start <= previous.end + 0.1:
            prior = previous.text.split()
            left = [_word_key(word) for word in prior]
            right = [_word_key(word) for word in words]
            for length in range(min(len(left), len(right)), 0, -1):
                if left[-length:] == right[:length]:
                    if length >= 2 or length == len(right):
                        overlap = length
                    break
        if overlap == len(words):
            if result:
                result[-1].end = max(result[-1].end, cue.end)
        else:
            result.append(Cue(cue.start, cue.end, " ".join(words[overlap:])))
        previous = cue
    return result


def load_transcript(path: Path) -> list[Cue]:
    """Load SRT, WebVTT, or timestamped JSON; plain text is intentionally rejected."""
    path = Path(path)
    raw = path.read_text(encoding="utf-8-sig")
    extension = path.suffix.lower()
    cues: list[Cue] = []
    if extension == ".json":
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid transcript JSON: {exc}") from exc
        if isinstance(entries, dict):
            entries = entries.get("segments")
        if not isinstance(entries, list):
            raise ValueError("Transcript JSON must be a list or a Whisper object with a segments list.")
        for number, entry in enumerate(entries, 1):
            if not isinstance(entry, dict) or "start" not in entry or "text" not in entry:
                raise ValueError(f"Transcript item {number} needs start, text, and end or duration.")
            end = entry.get("end")
            if end is None and "duration" in entry:
                try:
                    if isinstance(entry["duration"], bool):
                        raise ValueError
                    end = float(entry["start"]) + float(entry["duration"])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"Transcript item {number}: duration must be a number.") from exc
            cues.append(_cue(entry["start"], end, entry["text"], f"Transcript item {number}"))
    elif extension in {".srt", ".vtt"}:
        active: tuple[float, float, int] | None = None
        payload: list[str] = []
        number = 0
        ignored_block = False

        def finish() -> None:
            nonlocal active, payload
            if active is not None:
                text = _clean_text(" ".join(payload))
                if text:
                    start, end, cue_number = active
                    cues.append(_cue(start, end, text, f"Subtitle cue {cue_number}"))
            active, payload = None, []

        # Some YouTube VTT cues begin with a whitespace-only line or contain no
        # text. Blank-block splitting mistakes those lines for cue boundaries.
        lines = raw.splitlines()
        for index, line in enumerate(lines):
            if ignored_block:
                if not line.strip():
                    ignored_block = False
                continue
            if active is None and re.match(r"^(?:NOTE|STYLE|REGION)(?:\s|$)", line.strip()):
                ignored_block = True
                continue
            if "-->" in line:
                finish()
                number += 1
                match = _TIMING.search(line)
                if match is None:
                    raise ValueError(f"Subtitle cue {number} has an invalid timestamp range.")
                start, end = _seconds(match.group("start")), _seconds(match.group("end"))
                _cue(start, end, "timestamp validation", f"Subtitle cue {number}")
                active = (start, end, number)
            elif active is not None:
                if not _clean_text(" ".join(payload)) and index + 1 < len(lines) and "-->" in lines[index + 1]:
                    # A cue identifier after a blank previous cue is metadata,
                    # rather than that empty cue's spoken text.
                    continue
                if not line.strip() and _clean_text(" ".join(payload)):
                    finish()
                else:
                    payload.append(line)
        finish()
    else:
        raise ValueError("Use a timestamped .srt, .vtt, or .json transcript; plain text cannot be aligned to source slides.")
    if not cues:
        raise ValueError("The transcript contains no usable timestamped captions.")
    if extension == ".vtt" and _INLINE_TIMESTAMP.search(raw):
        return _deduplicate_rolling(cues)
    return sorted(cues, key=lambda cue: (cue.start, cue.end))


def probe_video(path: Path) -> float:
    """Return the source duration, requiring ffprobe and a readable local video."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Video does not exist: {path}")
    try:
        completed = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ffprobe is required to read video duration. Install FFmpeg and put ffprobe on PATH.") from exc
    if completed.returncode:
        raise RuntimeError(f"ffprobe could not read {path.name}: {completed.stderr.strip()[:500]}")
    try:
        duration = float(json.loads(completed.stdout)["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"ffprobe did not report a usable duration for {path.name}.") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Video duration must be finite and positive: {path.name}")
    return duration


def _choose_subtitle(info: dict, language: str) -> tuple[str | None, bool]:
    language = language.casefold().replace("_", "-")
    for automatic, key in ((False, "subtitles"), (True, "automatic_captions")):
        available = info.get(key) or {}
        candidates = [code for code, tracks in available.items() if tracks]
        exact = [code for code in candidates if code.casefold().replace("_", "-") == language]
        variants = [code for code in candidates if code.casefold().replace("_", "-").split("-")[0] == language.split("-")[0]]
        if exact or variants:
            return sorted(exact or variants, key=lambda code: (len(code), code))[0], automatic
    return None, False


class _QuietLogger:
    def debug(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass


def _download_error(kind: str, exc: Exception) -> str:
    message = str(exc).replace("\n", " ")[:500]
    return (
        f"Could not obtain YouTube {kind}: {message}. "
        "YouTube may block automated access or require sign-in. "
        "Use a local video and timestamped SRT/VTT/JSON transcript instead."
    )


def _downloaded_path(info: dict, workdir: Path, extensions: set[str]) -> str | None:
    possible: list[Path] = []
    for item in info.get("requested_downloads") or []:
        if item.get("filepath"):
            possible.append(Path(item["filepath"]))
    for key in ("filepath", "_filename"):
        if info.get(key):
            possible.append(Path(info[key]))
    possible.extend(sorted(workdir.glob("source.*"), key=lambda path: path.stat().st_mtime, reverse=True))
    for path in possible:
        if path.is_file() and path.suffix.lower() in extensions:
            return str(path.resolve())
    return None


def fetch_youtube(url: str, workdir: Path, language: str = "en", download_video: bool = True) -> dict:
    """Fetch one video's metadata, matching captions, and optionally original visuals.

    Subtitle and video failures are independent: unavailable captions never prevent
    a permitted video download, and a caption-only book can survive a video failure.
    No browser cookies or account credentials are read automatically.
    """
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError("YouTube input requires yt-dlp. Install this project with its dependencies, or provide local files.") from exc
    if not language.strip():
        raise ValueError("Subtitle language cannot be empty.")
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    common = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "logger": _QuietLogger(), "outtmpl": str(workdir / "source.%(ext)s"),
        "retries": 2, "fragment_retries": 2,
    }
    try:
        with yt_dlp.YoutubeDL({**common, "skip_download": True}) as downloader:
            info = downloader.extract_info(url, download=False)
    except Exception as exc:
        raise RuntimeError(_download_error("metadata", exc)) from exc
    if not isinstance(info, dict) or info.get("_type") in {"playlist", "multi_video"}:
        raise RuntimeError("Provide a single YouTube video URL. Playlist downloads are disabled.")
    # Reusing an output folder for another URL must never reuse the first
    # video's source.mp4 just because yt-dlp sees an existing filename.
    source_identity = str(info.get("id") or info.get("webpage_url") or url)
    media_dir = workdir / ("video-" + hashlib.sha256(source_identity.encode()).hexdigest()[:16])
    media_dir.mkdir(exist_ok=True)
    common["outtmpl"] = str(media_dir / "source.%(ext)s")
    warnings: list[str] = []
    try:
        duration = float(info.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if not math.isfinite(duration) or duration <= 0:
        duration = 0.0
        warnings.append("YouTube did not provide a duration; local video or caption timestamps will determine it.")
    selected, automatic = _choose_subtitle(info, language)
    transcript_path: str | None = None
    if selected is None:
        warnings.append(f"No {language} captions were available. Supply a local timestamped SRT/VTT/JSON transcript.")
    else:
        if automatic:
            warnings.append("The source captions are automatically generated and can contain recognition errors.")
        try:
            options = {
                **common, "skip_download": True, "writesubtitles": not automatic,
                "writeautomaticsub": automatic, "subtitleslangs": [re.escape(selected)],
                "subtitlesformat": "vtt/srt",
            }
            with yt_dlp.YoutubeDL(options) as downloader:
                caption_info = downloader.extract_info(url, download=True)
            requested = (caption_info or {}).get("requested_subtitles") or {}
            for track in requested.values():
                if track.get("filepath") and Path(track["filepath"]).is_file() and Path(track["filepath"]).suffix.lower() in {".vtt", ".srt"}:
                    transcript_path = str(Path(track["filepath"]).resolve())
                    break
            if transcript_path is None:
                for extension in ("vtt", "srt"):
                    candidate = media_dir / f"source.{selected}.{extension}"
                    if candidate.is_file():
                        transcript_path = str(candidate)
                        break
            if transcript_path is None:
                warnings.append("YouTube listed captions but did not save a transcript. Supply a local timestamped transcript.")
        except Exception as exc:
            warnings.append(_download_error("captions", exc))
    video_path: str | None = None
    if download_video:
        try:
            with yt_dlp.YoutubeDL({
                **common,
                "format": "bestvideo[height<=720]/best[height<=720]/bestvideo[height<=?720]/best[height<=?720]",
                "writesubtitles": False, "writeautomaticsub": False,
            }) as downloader:
                video_info = downloader.extract_info(url, download=True)
            video_path = _downloaded_path(video_info or {}, media_dir, {".mp4", ".webm", ".mkv", ".mov", ".avi"})
            if video_path is None:
                warnings.append("YouTube did not save a usable video. Supply a local video to capture its slides.")
        except Exception as exc:
            warnings.append(_download_error("video", exc))
    return {
        "title": str(info.get("title") or "Video reading book"),
        "source_url": str(info.get("webpage_url") or url),
        "duration": duration, "video_path": video_path,
        "transcript_path": transcript_path, "warnings": warnings,
    }
