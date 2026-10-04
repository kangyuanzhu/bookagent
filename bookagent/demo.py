"""A small, explicitly synthetic lesson for exercising the local pipeline."""

from pathlib import Path
import json
import shutil
import subprocess

from PIL import Image, ImageDraw, ImageFont


_INK = "#162b3d"
_TEAL = "#167c80"
_PAPER = "#f7f4ed"
_LIGHT_TEAL = "#70cdd0"
_SECONDS_PER_SLIDE = 12

_LESSON = (
    (
        "Start with the source",
        "Welcome to this synthetic sample lesson about the Video Book Agent. These diagrams were created for a local demonstration; they are not slides from the linked YouTube video. The agent starts with a video and timestamped captions. It combines original video visuals with short reading notes, then exports a book you can scan. Captions supply the presenter's words, while timestamps connect each page to its source interval. Keep the original material available so that readers can revisit examples, qualifications, and passages that deserve more attention than a compact excerpt can provide.",
    ),
    (
        "Keep time and words together",
        "Timestamped captions let the agent connect speech with the visuals shown at that time. A caption has a start, an end, and its text. The timeline stays in chronological order, including pauses between captions. Page notes retain the complete caption text assigned to that page. The readable excerpt may be much shorter, but the source is still available in PowerPoint notes and the HTML transcript panel. Overlapping captions need careful treatment because repeated context can appear in rolling subtitle windows. The ingestion step removes matching repeated context conservatively before the page planner runs.",
    ),
    (
        "Capture the actual slides",
        "The visual pipeline samples frames from the supplied video, then compares neighboring images to identify distinct visual states. This is a practical approximation of slide changes, not a perfect understanding of the recording. A sampling interval can miss a slide displayed only briefly. A crop can focus on the slide region when the recording also contains a speaker panel. Each book page uses a captured source image that was present during its interval. The agent does not invent a replacement slide when no suitable source image is available; that page is presented as transcript only.",
    ),
    (
        "Plan a readable page budget",
        "The page planner treats the requested page count as a maximum, including the cover and reading guide. It balances the amount of source text with elapsed video time and prefers useful boundaries near slide changes. A short transcript produces fewer pages rather than empty filler. When the page budget is smaller than the number of detected visual states, several original slides may share a reading interval and only the most representative image is pictured. Every retained caption still appears in the source notes. The generated manifest reports the actual page count and visual coverage for review.",
    ),
    (
        "Ground notes in evidence",
        "With an API key, the agent asks a model to summarize the presenter's statements using the assigned transcript and, optionally, the captured image. It requests a title, a concise summary, short bullets, a takeaway, and any uncertainty. Evidence quotes are checked against the page transcript, and the visible text must fit its word budget. These checks support review but do not prove every statement is correct. This synthetic demo uses the separate extractive mode: it selects verbatim transcript excerpts without calling a model and clearly labels them as excerpts rather than AI presenter summaries.",
    ),
    (
        "Match pages to reading time",
        "A reading target helps keep the book compact. For example, two hundred pages in one hour leave about eighteen seconds per page on average. That estimate includes reading short notes and scanning an image; a dense diagram or equation may take longer. The agent budgets visible prose and provides an estimated reading time using stated assumptions. Full transcript notes remain available, but reading every source word is a different activity from scanning the compact book. If the requested time cannot support the page count, choose fewer pages or allow more reading time before generating the summaries.",
    ),
    (
        "Export for your reading style",
        "The same planned book can be exported as PowerPoint, PDF, and HTML. PowerPoint is useful for presentation and keeps the full source captions in speaker notes. PDF provides a fixed layout for quick reading and sharing. HTML offers a searchable reading view with expandable transcript text. A JSON manifest preserves the page structure, timestamps, metadata, and warnings for later reuse. Each content page shows its source interval, and a supported remote source link can jump back to that point in the original video. The formats share the same grounded content rather than generating separate interpretations.",
    ),
    (
        "Review before relying on it",
        "After generation, scan the reading guide and a few pages to check that the visuals and captions align. Review warnings about missing captions, sampled slide changes, and visual states that could not be separately pictured. Use source timestamps to revisit unclear explanations or important claims. Automatically generated captions can contain recognition errors, and concise summaries can omit details that matter to a particular reader. This local synthetic sample demonstrates the workflow and exports without making any claim to summarize the user's four hour YouTube recording. Processing that recording requires its own available captions and video, or supplied local files.",
    ),
)


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    names = (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        "Arial.ttf",
    )
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _center(draw: ImageDraw.ImageDraw, text: str, x: float, y: float, fill: str, size: int = 28) -> None:
    font = _font(size, bold=True)
    bounds = draw.textbbox((0, 0), text, font=font)
    draw.text((x - (bounds[2] - bounds[0]) / 2, y), text, font=font, fill=fill)


def _arrow(draw: ImageDraw.ImageDraw, left: int, right: int, y: int, color: str) -> None:
    draw.line((left, y, right - 12, y), fill=color, width=5)
    draw.polygon(((right, y), (right - 18, y - 10), (right - 18, y + 10)), fill=color)


def _draw_slide(index: int, title: str, path: Path) -> None:
    image = Image.new("RGB", (1280, 720), _PAPER)
    draw = ImageDraw.Draw(image)
    draw.text((64, 34), f"VIDEO BOOK AGENT  /  SYNTHETIC SAMPLE  /  {index + 1:02} OF 08", font=_font(20, True), fill=_TEAL)
    draw.text((64, 87), title, font=_font(46, True), fill=_INK)
    draw.rectangle((64, 157, 1216, 162), fill=_TEAL)
    dark = index % 2 == 0
    background, foreground, accent = (_INK, _PAPER, _LIGHT_TEAL) if dark else (_PAPER, _INK, _TEAL)
    draw.rounded_rectangle((64, 194, 1216, 622), radius=16, fill=background, outline=_TEAL, width=2)

    def box(x: int, y: int, width: int, height: int, label: str, detail: str = "") -> None:
        draw.rounded_rectangle((x, y, x + width, y + height), radius=12, outline=accent, width=3)
        _center(draw, label, x + width / 2, y + height / 2 - 28, foreground, 28)
        if detail:
            _center(draw, detail, x + width / 2, y + height / 2 + 15, accent, 18)

    if index == 0:
        box(124, 310, 285, 150, "Video", "original visuals")
        box(497, 310, 285, 150, "Captions", "timestamped speech")
        box(870, 310, 285, 150, "Reading book", "scan + revisit")
        _arrow(draw, 422, 484, 385, accent)
        _arrow(draw, 795, 857, 385, accent)
    elif index == 1:
        _center(draw, "One continuous source timeline", 640, 244, foreground, 30)
        draw.line((140, 360, 1140, 360), fill=accent, width=4)
        for item in range(8):
            x = 140 + item * 125
            draw.rectangle((x, 331, x + 108, 388), fill=accent)
            _center(draw, f"{item * 12}s", x + 54, 413, foreground, 22)
        _center(draw, "Every caption remains in the source notes", 640, 510, foreground, 25)
    elif index == 2:
        for item, x in enumerate((128, 501, 874)):
            box(x, 275, 277, 210, f"Slide {item + 1}", "captured from video")
            for offset in (0, 1, 2):
                draw.line((x + 40, 425 + offset * 13, x + 237, 425 + offset * 13), fill=accent, width=3)
        _arrow(draw, 416, 487, 375, accent)
        _arrow(draw, 789, 860, 375, accent)
    elif index == 3:
        for item in range(8):
            x, y = 150 + (item % 4) * 255, 260 + (item // 4) * 145
            box(x, y, 215, 108, f"Page {item + 3}", "source + notes")
        _center(draw, "A maximum page budget, with no empty filler", 640, 557, foreground, 24)
    elif index == 4:
        box(122, 308, 286, 160, "Transcript", "source statements")
        box(498, 308, 286, 160, "Reading notes", "short and qualified")
        box(874, 308, 286, 160, "Evidence check", "quotes + word limit")
        _arrow(draw, 422, 485, 388, accent)
        _arrow(draw, 798, 861, 388, accent)
    elif index == 5:
        draw.text((144, 254), "Example source video", font=_font(25, True), fill=foreground)
        draw.rounded_rectangle((144, 297, 1116, 353), radius=8, fill=accent)
        draw.text((144, 393), "Example quick reading target", font=_font(25, True), fill=foreground)
        draw.rounded_rectangle((144, 436, 387, 492), radius=8, fill=accent)
        _center(draw, "4 hours → 1 hour  /  200 pages ≈ 18 seconds each", 640, 549, foreground, 25)
    elif index == 6:
        for x, label, detail in ((145, "PowerPoint", "full speaker notes"), (505, "PDF", "fixed reading layout"), (865, "HTML", "search + transcripts")):
            box(x, 286, 270, 214, label, detail)
        _center(draw, "One book manifest, several ways to read", 640, 555, foreground, 25)
    else:
        for y, text in ((270, "Check visual and caption alignment"), (375, "Revisit important claims at the source"), (480, "Read limitations before sharing")):
            draw.rounded_rectangle((151, y, 191, y + 40), radius=5, outline=accent, width=3)
            draw.line((159, y + 20, 169, y + 30, 185, y + 10), fill=accent, width=4)
            draw.text((221, y + 3), text, font=_font(29, True), fill=foreground)
    draw.text((64, 665), "Synthetic lesson for this demo. Not the linked YouTube video.", font=_font(21), fill=_TEAL)
    image.save(path, optimize=True)


def _concat_quote(path: Path) -> str:
    # FFmpeg's concat parser has its own quoting rules, independent of a shell.
    # Forward slashes also keep Windows paths out of its backslash-escape syntax.
    return "'" + path.resolve().as_posix().replace("'", "'\\''") + "'"


def _make_video(slides: list[Path], source_dir: Path) -> Path | None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        print("FFmpeg/ffprobe are unavailable; building the synthetic sample from captions only.", flush=True)
        return None
    manifest = source_dir / "slides.ffconcat"
    entries = ["ffconcat version 1.0"]
    for slide in slides:
        entries.extend((f"file {_concat_quote(slide)}", f"duration {_SECONDS_PER_SLIDE}"))
    # The repeated final entry makes the previous entry's duration effective.
    entries.append(f"file {_concat_quote(slides[-1])}")
    manifest.write_text("\n".join(entries) + "\n", encoding="utf-8")
    video = source_dir / "synthetic-lesson.mp4"
    completed = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "concat", "-safe", "0",
         "-i", str(manifest), "-vf", "fps=1", "-t", str(len(slides) * _SECONDS_PER_SLIDE),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(video)],
        capture_output=True, text=True, check=False, timeout=60,
    )
    if completed.returncode:
        raise RuntimeError(f"Could not create the synthetic demo video: {completed.stderr[-1000:].strip()}")
    return video


def run_demo(outdir: Path) -> None:
    """Generate eight synthetic diagrams and build a ten-page, no-API sample."""
    from .cli import _parser, build_book

    out = Path(outdir).resolve()
    source_dir = out / "work" / "demo-source"
    source_dir.mkdir(parents=True, exist_ok=True)
    print("Creating a synthetic sample lesson; this is not the user's YouTube video.", flush=True)
    slides, segments = [], []
    for index, (title, text) in enumerate(_LESSON):
        slide = source_dir / f"synthetic-slide-{index + 1:02}.png"
        _draw_slide(index, title, slide)
        slides.append(slide)
        start = index * _SECONDS_PER_SLIDE
        segments.append({"start": start, "end": start + _SECONDS_PER_SLIDE - 0.5, "text": text})
    transcript = source_dir / "synthetic-transcript.json"
    transcript.write_text(json.dumps({
        "synthetic": True,
        "description": "Demonstration material created locally; not captions from the user's YouTube video.",
        "segments": segments,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    video = _make_video(slides, source_dir)
    argv = ["build", "--transcript", str(transcript), "--duration", str(len(slides) * _SECONDS_PER_SLIDE),
            "--provider", "extractive", "--title", "Video Book Agent — synthetic sample", "--pages", "10",
            "--reading-minutes", "5", "--frame-interval", "3", "--out", str(out)]
    if video is not None:
        argv.extend(("--video", str(video)))
    build_book(_parser().parse_args(argv))
