"""Partition a complete transcript into traceable, visually grounded pages.

The requested page count is an upper bound. Sparse transcripts receive fewer
pages, and original slides may be combined when the requested budget is small.
Captions are never split: overlapping captions belong to the page containing
their start, so a caption's end can extend beyond that page's reading interval.
"""

from bisect import bisect_left, bisect_right
import math
import re

from .models import Cue, Frame, Page, timestamp


_CJK = "\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af"
_WORDS = re.compile(rf"[{_CJK}]|[^\W_{_CJK}]+(?:['’][^\W_{_CJK}]+)*", re.UNICODE)
_MIN_SOURCE_WORDS = 40


def count_words(text: str) -> int:
    """Count whitespace-language words and individual CJK reading units."""
    return len(_WORDS.findall(text))


def _finite_number(value: float, label: str) -> None:
    try:
        finite = math.isfinite(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not finite:
        raise ValueError(f"{label} must be a finite number")


def _nearest_change(time: float, changes: list[float]) -> float:
    if not changes:
        return math.inf
    index = bisect_left(changes, time)
    candidates = changes[max(0, index - 1):index + 1]
    return min(abs(change - time) for change in candidates)


def _representative_frame(
    frames: list[Frame], start: float, end: float, duration: float
) -> str | None:
    """A sampled slide persists until the next slide-change representative."""
    selected = None
    longest_overlap = 0.0
    for index, frame in enumerate(frames):
        if frame.timestamp >= end:
            break
        slide_end = frames[index + 1].timestamp if index + 1 < len(frames) else duration
        overlap = min(end, slide_end) - max(start, frame.timestamp)
        # Strict > gives deterministic earlier-slide preference on equal dwell.
        if overlap > longest_overlap:
            longest_overlap = overlap
            selected = frame.path
    return selected


def plan_pages(
    cues: list[Cue], frames: list[Frame], duration: float, target_pages: int
) -> list[Page]:
    """Return content pages numbered from 3; callers add cover and overview.

    Page intervals form a continuous partition of 0..duration. Every input cue
    appears exactly once, unchanged, in a timestamp-prefixed source transcript.
    Empty captions join neighboring content rather than creating blank pages;
    an entirely empty transcript returns no content pages.

    The count is capped at roughly one page per forty source reading units and
    by distinct, nonempty caption start times. Boundaries balance source length
    and elapsed time, with a preference for nearby original slide changes.
    """
    if isinstance(target_pages, bool) or not isinstance(target_pages, int) or target_pages <= 0:
        raise ValueError("target_pages must be a positive integer")
    _finite_number(duration, "duration")
    if duration <= 0:
        raise ValueError("duration must be positive")
    for cue in cues:
        _finite_number(cue.start, "cue start")
        _finite_number(cue.end, "cue end")
        if not 0 <= cue.start < cue.end <= duration:
            raise ValueError("each cue must satisfy 0 <= start < end <= duration")
        if not isinstance(cue.text, str):
            raise ValueError("cue text must be a string")
    for frame in frames:
        _finite_number(frame.timestamp, "frame timestamp")
        if not 0 <= frame.timestamp <= duration:
            raise ValueError("frame timestamp must fall within the video duration")

    ordered_cues = sorted(cues, key=lambda cue: (cue.start, cue.end))
    ordered_frames = sorted(frames, key=lambda frame: frame.timestamp)
    if not any(cue.text.strip() for cue in ordered_cues):
        return []

    # Equal-start captions cannot occupy separate, positive-length intervals.
    # Do not merge transitive overlaps: rolling captions can overlap for hours.
    groups: list[list[Cue]] = []
    for cue in ordered_cues:
        if groups and groups[-1][0].start == cue.start:
            groups[-1].append(cue)
        else:
            groups.append([cue])

    word_prefix = [0]
    nonempty_prefix = [0]
    end_prefix = [0.0]
    for group in groups:
        words = sum(max(1, count_words(cue.text)) if cue.text.strip() else 0 for cue in group)
        word_prefix.append(word_prefix[-1] + words)
        nonempty_prefix.append(nonempty_prefix[-1] + int(words > 0))
        end_prefix.append(max(end_prefix[-1], *(cue.end for cue in group)))
    total_words = word_prefix[-1]
    page_count = min(target_pages, max(1, total_words // _MIN_SOURCE_WORDS), nonempty_prefix[-1])
    changes = [frame.timestamp for frame in ordered_frames]

    def gap(index: int) -> tuple[float, float]:
        following_start = groups[index][0].start
        return min(end_prefix[index], following_start), following_start

    def mass(index: int, time: float) -> float:
        return 0.85 * word_prefix[index] / total_words + 0.15 * time / duration

    base_times = [0.0]
    for index in range(1, len(groups)):
        lower, upper = gap(index)
        base_times.append((lower + upper) / 2)
    base_times.append(duration)
    base_masses = [mass(index, time) for index, time in enumerate(base_times)]

    cuts = [0]
    times = [0.0]
    for page_index in range(1, page_count):
        ideal = page_index / page_count
        previous = cuts[-1]
        remaining_pages = page_count - page_index
        # Require readable source on both sides, including the remaining pages.
        minimum = bisect_right(nonempty_prefix, nonempty_prefix[previous])
        maximum = min(
            len(groups) - 1,
            bisect_right(nonempty_prefix, nonempty_prefix[-1] - remaining_pages) - 1,
        )
        lower_index = max(minimum, bisect_left(base_masses, ideal - 0.7 / page_count))
        upper_index = min(maximum, bisect_right(base_masses, ideal + 0.7 / page_count) - 1)
        if lower_index > upper_index:
            nearest = max(minimum, min(maximum, bisect_left(base_masses, ideal)))
            candidate_indices = range(max(minimum, nearest - 1), min(maximum, nearest + 1) + 1)
        else:
            candidate_indices = range(lower_index, upper_index + 1)

        best: tuple[float, int, float] | None = None
        for index in candidate_indices:
            lower, upper = gap(index)
            candidate_times = [base_times[index], upper]
            # A slide change in genuine silence is a particularly useful cut.
            if lower < upper:
                desired_time = (ideal - 0.85 * word_prefix[index] / total_words) * duration / 0.15
                first = bisect_left(changes, lower)
                last = bisect_right(changes, upper)
                nearest = bisect_left(changes, desired_time, first, last)
                for change_index in (nearest - 1, nearest):
                    if first <= change_index < last:
                        candidate_times.append(changes[change_index])
            for time in candidate_times:
                if time <= times[-1] or time >= duration:
                    continue
                deviation = (mass(index, time) - ideal) * page_count
                proximity = max(0.0, 1.0 - _nearest_change(time, changes) / 5.0)
                score = deviation * deviation - 0.24 * proximity
                candidate = (score, index, time)
                if best is None or candidate < best:
                    best = candidate
        if best is None:  # Valid nonempty cue starts guarantee this fallback.
            index = minimum
            time = groups[index][0].start
        else:
            _, index, time = best
        cuts.append(index)
        times.append(time)
    cuts.append(len(groups))
    times.append(duration)

    pages = []
    for index, (first, last) in enumerate(zip(cuts, cuts[1:])):
        source = "\n".join(
            f"[{timestamp(cue.start)}] {cue.text}"
            for group in groups[first:last]
            for cue in group
        )
        pages.append(Page(
            number=index + 3,
            title=f"Section {index + 1}",
            start=times[index],
            end=times[index + 1],
            summary="",
            bullets=[],
            takeaway="",
            frame_path=_representative_frame(ordered_frames, times[index], times[index + 1], duration),
            source_text=source,
        ))
    return pages
