"""Reassemble the local book from the saved assistant-session checkpoints.

No paid API call is required. Summary batches are authored from the source
captions in this session and checked against the same pipeline schema.
The required output/plenary/work files are not included in the Git repository.
For a reproducible fresh-clone example, run ``bookagent demo`` instead.
"""

from dataclasses import asdict, replace
from pathlib import Path
import json
import re

from PIL import Image, ImageStat

from bookagent.export import export_book
from bookagent.frames import _select_frames
from bookagent.models import Book, Frame, Page, timestamp
from bookagent.planner import _representative_frame
from bookagent.summarize import SummaryBatch, validate_result, visible_text, word_count


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "plenary"
WORK = OUT / "work"
DURATION = 14998
PROGRAM = "https://rdi.berkeley.edu/events/agentic-ai-summit-2026"


def verified_names(text: str) -> str:
    """Correct caption spellings using the official event program, not face inference."""
    for original, corrected in ((r"\bMika Spranger\b", "Michael Spranger"),
                                (r"\bMika\b", "Michael"),
                                (r"\bLee Tang\b", "Li Deng"),
                                (r"\bTang\b", "Deng"),
                                (r"\bBrad Olson\b", "Bradley Olson"),
                                (r"\bVojciech Zaremba\b", "Wojciech Zaremba")):
        text = re.sub(original, corrected, text)
    return text


def prepare_visuals() -> list[Frame]:
    """Extract the slide panel only when the video's fixed slide layout is visible."""
    destination = WORK / "slide-panels"
    destination.mkdir(exist_ok=True)
    frames = [Frame(**item) for item in json.loads((WORK / "frames.json").read_text())]
    prepared = []
    cropped = 0
    for frame in frames:
        with Image.open(frame.path) as image:
            if image.size != (1280, 720):
                prepared.append(frame)
                continue
            gray = image.convert("L")
            average = lambda box: ImageStat.Stat(gray.crop(box)).mean[0]
            # The recording places 16:9 slides at x=4,y=81,w=934,h=524,
            # beside a presenter inset, with black margins above and below.
            slide_layout = (average((0, 0, 1280, 70)) < 16
                            and average((0, 620, 1280, 720)) < 16
                            and average((8, 85, 930, 95)) > 35
                            and average((8, 590, 930, 600)) > 35)
            if slide_layout:
                output = destination / Path(frame.path).name
                if not output.exists():
                    image.crop((4, 81, 938, 605)).save(output, quality=94)
                prepared.append(Frame(frame.timestamp, str(output)))
                cropped += 1
            else:
                prepared.append(frame)
    # Ignore motion in the presenter inset when recognizing repeated slides.
    selected = _select_frames(prepared, 0.18)
    (WORK / "prepared-frames.json").write_text(json.dumps([asdict(f) for f in selected], indent=2))
    print(f"Prepared {len(selected)} distinct visuals; cropped {cropped} lecture captures to the original slide panel.")
    return selected


def main() -> None:
    required = [WORK / name for name in ("planned-pages.json", "frames.json", "captions.vtt",
                                       "batch-1.json", "batch-2.json", "batch-3.json",
                                       "summaries-1.json", "summaries-2.json", "summaries-3.json")]
    missing = [path.name for path in required if not path.is_file()]
    if missing:
        raise SystemExit("This local assembly script requires saved session checkpoints in output/plenary/work: "
                         + ", ".join(missing) + ". These are not distributed in the repository. Run 'bookagent demo' for a fresh-clone sample.")
    raw_pages = json.loads((WORK / "planned-pages.json").read_text())
    originals = [Page(**item) for item in raw_pages]
    merged = []
    for batch_index in range(1, 4):
        batch = [Page(**item) for item in json.loads((WORK / f"batch-{batch_index}.json").read_text())]
        result = SummaryBatch.model_validate_json((WORK / f"summaries-{batch_index}.json").read_text())
        validate_result(result, batch, 35)
        merged.extend(result.pages)
    if [s.page_id for s in merged] != [p.number for p in originals]:
        raise ValueError("Prepared summaries must cover all 198 content pages in order.")
    frames_path = WORK / "prepared-frames.json"
    frames = ([Frame(**item) for item in json.loads(frames_path.read_text())]
              if frames_path.exists() else prepare_visuals())
    pages = []
    for page, summary in zip(originals, merged):
        frame_path = _representative_frame(frames, page.start, page.end, DURATION)
        pages.append(replace(page, title=verified_names(summary.title), summary=verified_names(summary.summary),
                             bullets=[verified_names(b) for b in summary.bullets], takeaway=verified_names(summary.takeaway),
                             chapter=verified_names(summary.chapter), uncertainty=verified_names(summary.uncertainty),
                             frame_path=frame_path,
                             source_text=f"Summary based on source captions from {timestamp(page.start)} to {timestamp(page.end)}. Use the linked source timestamp to revisit this section."))
    overview = Page(2, "Your one-hour reading route", 0, DURATION,
                    "The afternoon connects safer agents, stronger learning and reasoning, embodied intelligence, enterprise deployment, and startup execution.",
                    ["Pages 3–68 · Foundations: security, learning, reasoning, memory.",
                     "Pages 69–86 · Research panel: architecture and safety.",
                     "Pages 87–155 · Robotics: models, scaling, autonomy, debate.",
                     "Pages 156–180 · Finance: evaluation, workflow design, deployment.",
                     "Pages 181–200 · Andrew Ng and Alfred Lin: startups and adoption."],
                    "Scan each visual, read its summary, and revisit only the passages you need.",
                    kind="overview", chapter="Reading guide",
                    source_text="Reading map follows the source recording in chronological order. Section transitions can fall inside a page.")
    cover = Page(1, "Agentic AI: Research, Robotics & Deployment", 0, DURATION,
                 "Berkeley RDI · Plenary Stage · August 1st afternoon session. A 200-page reading book from 4 hours, 9 minutes, 58 seconds of video.",
                 ["Original lecture slides and panel views.", "Concise presenter summaries with source timestamps."],
                 "Reading target: one hour.", kind="cover", chapter="Source recording",
                 source_text="Source: https://www.youtube.com/watch?v=Tcn5Yb2K0h4")
    all_pages = [cover, overview, *pages]
    prose_words = sum(word_count(visible_text(p)) for p in all_pages)
    estimate = prose_words / 180 + len(pages) * 6 / 60
    warnings = [
        "Presenter summaries were prepared in this assistant session from the YouTube captions. Original claims, predictions, and reported results are not independent verification.",
        "Video visuals were sampled every 12 seconds. Some short slides or animations may be missed, and each page pictures one representative visual for its source interval.",
        "Lecture captures showing the recording's fixed slide layout are cropped to the original slide panel; panel pages retain the stage view.",
        "Section transitions can occur inside a page. Captions may contain recognition or spelling errors.",
        f"Speaker name spellings were checked against the official event program: {PROGRAM}",
        "One-hour reading is a target. The estimate covers summary prose and a six-second visual scan; dense text, figures, and equations can take longer.",
    ]
    metadata = {"provider": "assistant-session", "requested_pages": 200,
                "content_word_limit": 35, "visible_prose_words": prose_words,
                "estimated_reading_minutes": round(estimate, 1),
                "reading_assumptions": "180 prose words/minute + 6 seconds per content visual; dense image text takes additional time.",
                "original_video": str(WORK / "source" / "video.mp4"),
                "source_caption_file": str(WORK / "captions.vtt"),
                "source_caption_cues": 5215, "pictured_visuals": len({p.frame_path for p in pages}),
                "embed_transcript": False, "summary_evidence": "Validated against page-local source captions; checkpoints remain in work/summaries-*.json.",
                "source_reference": "https://www.youtube.com/watch?v=Tcn5Yb2K0h4", "program_reference": PROGRAM}
    book = Book(cover.title, metadata["source_reference"], DURATION, all_pages, 60, warnings, metadata)
    outputs = export_book(book, OUT)
    print(f"Created {len(all_pages)} pages with {prose_words} prose words; estimated skim {estimate:.1f} minutes.")
    print(json.dumps({name: str(path) for name, path in outputs.items()}, indent=2))


if __name__ == "__main__":
    main()
