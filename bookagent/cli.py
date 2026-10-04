"""Command line entry point and pipeline orchestration."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import shutil
import sys

from .models import Book, Page, timestamp
from .summarize import OpenAISummarizer, extractive_pages, summarize_overview, visible_text, word_count


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Turn a timestamped video transcript and original visuals into a reading book.")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="Build from a YouTube URL or local video/transcript.")
    build.add_argument("source", nargs="?", help="YouTube URL or local video path; quote URLs containing &.")
    build.add_argument("--video", type=Path, help="Local video to capture slides from.")
    build.add_argument("--transcript", type=Path, help="Timestamped .srt, .vtt or .json captions.")
    build.add_argument("--title", help="Book title; defaults to the video title.")
    build.add_argument("--pages", type=int, default=200, help="Maximum TOTAL pages, including cover + overview (default: 200).")
    build.add_argument("--reading-minutes", type=float, default=60, help="Approximate reading-time target (default: 60).")
    build.add_argument("--duration", type=float, help="Full source duration in seconds when only captions are supplied.")
    build.add_argument("--language", default="en", help="YouTube caption language (summaries are in English).")
    build.add_argument("--provider", choices=("openai", "extractive"), default="openai",
                       help="AI summaries, or clearly labeled verbatim excerpts without an API key.")
    build.add_argument("--model", default=os.environ.get("BOOKAGENT_MODEL", "gpt-6-astra"))
    build.add_argument("--workers", type=int, default=3, help="Parallel summary requests, 1-8.")
    build.add_argument("--transcript-only", action="store_true", help="Skip video downloads and slide capture.")
    build.add_argument("--text-only-ai", action="store_true", help="Summarize captions without sending frames to the API.")
    build.add_argument("--frame-interval", type=float, default=12, help="Seconds between candidate slide captures.")
    build.add_argument("--crop", help="Slide region as normalized x,y,width,height; e.g. 0,0,0.75,1.")
    build.add_argument("--formats", default="pptx,pdf,html", help="Comma-separated pptx,pdf,html.")
    build.add_argument("--out", type=Path, default=Path("output/book"))
    demo = commands.add_parser("demo", help="Generate and process a synthetic sample lesson, without an API key.")
    demo.add_argument("--out", type=Path, default=Path("output/demo"))
    commands.add_parser("doctor", help="Check local tools and API key presence without exposing secrets.")
    return parser


def _positive(value: float, name: str) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number.")


def build_book(args) -> Book:
    from .export import export_book
    from .frames import extract_frames
    from .ingest import fetch_youtube, load_transcript, probe_video
    from .planner import plan_pages

    if args.pages < 3:
        raise ValueError("--pages must be at least 3 (cover, overview, and one content page).")
    _positive(args.reading_minutes, "--reading-minutes")
    _positive(args.frame_interval, "--frame-interval")
    if args.duration is not None:
        _positive(args.duration, "--duration")
    if not 1 <= args.workers <= 8:
        raise ValueError("--workers must be between 1 and 8.")
    formats = tuple(dict.fromkeys(f.strip().lower() for f in args.formats.split(",")))
    if not formats or any(f not in ("pptx", "pdf", "html") for f in formats):
        raise ValueError("--formats accepts pptx,pdf,html.")
    crop = None
    if args.crop:
        try:
            crop = tuple(float(x) for x in args.crop.split(","))
        except ValueError as error:
            raise ValueError("--crop needs four normalized numbers: x,y,width,height.") from error
        if len(crop) != 4 or not all(math.isfinite(x) for x in crop) or not (
            crop[0] >= 0 and crop[1] >= 0 and crop[2] > 0 and crop[3] > 0
            and crop[0] + crop[2] <= 1 and crop[1] + crop[3] <= 1
        ):
            raise ValueError("--crop must describe a positive region inside the frame.")
    if args.provider == "openai" and not os.environ.get("OPENAI_API_KEY"):
        raise ValueError("Set OPENAI_API_KEY in your environment for AI summaries. Run 'bookagent demo' to try it, or use --provider extractive for labeled transcript excerpts.")
    if not args.source and not args.video and not args.transcript:
        raise ValueError("Supply a YouTube URL, --video, or --transcript.")

    out = args.out.resolve()
    work = out / "work"
    work.mkdir(parents=True, exist_ok=True)
    source = args.source or (str(args.video.resolve()) if args.video else str(args.transcript.resolve()))
    video = args.video.resolve() if args.video else None
    transcript = args.transcript.resolve() if args.transcript else None
    warnings: list[str] = []
    metadata: dict = {}
    title = args.title or "Video reading book"
    duration = args.duration
    if args.source and args.source.startswith(("https://", "http://")):
        # A supplied local transcript/video avoids unnecessary remote access.
        if transcript is None or (video is None and not args.transcript_only):
            print("Fetching video metadata, captions and source visuals…", flush=True)
            remote = fetch_youtube(args.source, work / "source", language=args.language,
                                   download_video=not args.transcript_only and video is None)
            title = args.title or remote.get("title") or title
            duration = duration or remote.get("duration")
            video = video or (Path(remote["video_path"]) if remote.get("video_path") else None)
            transcript = transcript or (Path(remote["transcript_path"]) if remote.get("transcript_path") else None)
            warnings.extend(remote.get("warnings", []))
            metadata["input"] = {k: remote.get(k) for k in ("title", "source_url", "duration")}
    elif args.source:
        video = video or Path(args.source).resolve()
        title = args.title or video.stem
    if transcript is None:
        raise ValueError("No captions were available. Supply --transcript with a timestamped SRT, VTT or JSON file. The agent does not invent transcripts or transcribe audio automatically.")
    cues = load_transcript(transcript)
    caption_end = max(c.end for c in cues)
    if video:
        video_duration = probe_video(video)
        if args.duration and args.duration < video_duration - 0.1:
            raise ValueError("--duration is shorter than the supplied video; use its full duration.")
        duration = duration or video_duration
    if duration is None:
        duration = caption_end
        warnings.append("Duration inferred from the final caption; completeness of the original video is unknown.")
    if duration < caption_end:
        warnings.append("Captions extend beyond the reported source duration; the timeline was extended to retain them.")
        duration = caption_end
    _positive(duration, "source duration")
    frames = []
    if video and not args.transcript_only:
        print("Capturing and deduplicating source slides…", flush=True)
        frames = extract_frames(video, work / "frames", interval=args.frame_interval, crop=crop)
        warnings.append(f"Slide changes are approximated from {args.frame_interval:g}-second samples; short-lived slides may be missed.")
    else:
        warnings.append("Transcript-only book: original slide visuals are unavailable.")

    pages = plan_pages(cues, frames, duration, args.pages - 2)
    if len(pages) + 2 < args.pages:
        warnings.append(f"Source supports {len(pages) + 2} pages under the sparsity guard; the {args.pages}-page budget was not padded.")
    # Budget 180 words/minute, six seconds to scan each image, and front matter.
    visual_seconds = 6 if frames else 0
    budget = args.reading_minutes * 180 - 180 - len(pages) * visual_seconds * 3
    max_words = min(65, int(budget / len(pages)))
    if max_words < 18:
        raise ValueError("Reading target is too short for this page count. Reduce --pages or increase --reading-minutes.")
    print(f"Planning {len(pages) + 2} pages; up to {max_words} visible words per content page.", flush=True)
    if args.provider == "openai":
        summarizer = OpenAISummarizer(args.model, work / "summaries", workers=args.workers,
                                     include_images=not args.text_only_ai)
        pages = summarizer.summarize(pages, max_words,
                                    lambda done, total: print(f"Summarized {done}/{total} pages", flush=True))
        overview = summarize_overview(summarizer, pages, duration)
    else:
        pages = extractive_pages(pages, max_words)
        warnings.append("Extractive mode: visible text consists of transcript excerpts, not AI presenter summaries.")
        overview = Page(2, "Your reading route", 0, duration,
                        "Follow the pages in order. Each excerpt links to its source interval. Read full captions in PowerPoint notes or expand them in HTML.",
                        ["Scan the original visual, then read the excerpt.", "Revisit unclear passages using the timestamp."],
                        "This sample uses verbatim excerpts.", kind="overview", chapter="Reading guide")
    cover = Page(1, title, 0, duration,
                 f"{len(pages) + 2} pages from {timestamp(duration)} of video. Target reading time: {args.reading_minutes:g} minutes.",
                 ["Original video visuals with concise reading notes.", "Full timestamped source captions preserved."],
                 "Read, scan, and jump back to the source.", kind="cover")
    represented = {p.frame_path for p in pages if p.frame_path}
    if len(represented) < len(frames):
        warnings.append(f"{len(frames) - len(represented)} detected visual states are not separately pictured under this page budget; captured images remain in work/frames.")
    all_pages = [cover, overview, *pages]
    prose_words = sum(word_count(visible_text(p)) for p in all_pages)
    estimate = prose_words / 180 + len(pages) * visual_seconds / 60
    metadata.update({"provider": args.provider, "model": args.model if args.provider == "openai" else None,
                     "requested_pages": args.pages, "content_word_limit": max_words,
                     "caption_cues": len(cues), "source_words": sum(word_count(c.text) for c in cues),
                     "captured_visual_states": len(frames), "pictured_visual_states": len(represented),
                     "visible_prose_words": prose_words, "estimated_reading_minutes": round(estimate, 1),
                     "reading_assumptions": "180 prose words/minute + 6 seconds per pictured content page. Text inside images and full transcript notes are excluded.",
                     "language": "English", "transcript_path": str(transcript)})
    warnings.append("Reading time is an estimate; dense slide text, equations and expanded transcript notes take additional time.")
    book = Book(title, source, duration, all_pages, args.reading_minutes, warnings, metadata)
    paths = export_book(book, out, formats)
    print(f"Created {len(all_pages)} pages. Estimated quick reading: {estimate:.1f} minutes.", flush=True)
    for format_name, path in paths.items():
        print(f"{format_name}: {path}")
    for warning in warnings:
        print(f"Note: {warning}")
    return book


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            print(json.dumps({"ffmpeg": shutil.which("ffmpeg"), "ffprobe": shutil.which("ffprobe"),
                              "OPENAI_API_KEY_present": bool(os.environ.get("OPENAI_API_KEY")),
                              "model": os.environ.get("BOOKAGENT_MODEL", "gpt-6-astra")}, indent=2))
        elif args.command == "demo":
            from .demo import run_demo
            run_demo(args.out)
        else:
            build_book(args)
        return 0
    except KeyboardInterrupt:
        print("Interrupted. Completed summary checkpoints are saved; rerun the same command to resume.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"bookagent: {error}", file=sys.stderr)
        return 1
