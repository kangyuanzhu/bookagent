import math

import pytest

from bookagent.models import Cue, Frame, timestamp
from bookagent.planner import count_words, plan_pages


def cue(start, end, marker, words=40):
    return Cue(start, end, f"{marker} " + " ".join(f"word{index}" for index in range(words - 1)))


def assert_coverage(pages, cues, duration):
    assert pages[0].start == 0
    assert pages[-1].end == duration
    assert all(page.start < page.end for page in pages)
    assert all(left.end == right.start for left, right in zip(pages, pages[1:]))
    expected = [f"[{timestamp(item.start)}] {item.text}" for item in sorted(cues, key=lambda item: (item.start, item.end))]
    actual = [line for page in pages for line in page.source_text.splitlines()]
    assert actual == expected
    assert [page.number for page in pages] == list(range(3, 3 + len(pages)))
    assert all(page.summary == "" and page.bullets == [] and page.takeaway == "" for page in pages)


def test_all_source_cues_appear_once_and_inputs_are_not_mutated():
    cues = [cue(index * 10, index * 10 + 8, f"caption{index}") for index in range(12)]
    cues.reverse()
    frames = [Frame(70, "second.png"), Frame(0, "first.png")]
    original_cues = [(item.start, item.end, item.text) for item in cues]
    original_frames = [(item.timestamp, item.path) for item in frames]
    pages = plan_pages(cues, frames, 130, 5)
    assert len(pages) == 5
    assert_coverage(pages, cues, 130)
    assert [(item.start, item.end, item.text) for item in cues] == original_cues
    assert [(item.timestamp, item.path) for item in frames] == original_frames


def test_continuously_overlapping_captions_do_not_collapse_into_one_page():
    cues = [cue(index, index + 3, f"overlap{index}") for index in range(40)]
    pages = plan_pages(cues, [], 45, 10)
    assert len(pages) == 10
    assert_coverage(pages, cues, 45)


def test_identical_start_times_stay_together_and_empty_captions_do_not_create_pages():
    cues = [cue(10, 20, "first"), Cue(10, 25, ""), cue(10, 22, "second"), Cue(30, 31, "  ")]
    pages = plan_pages(cues, [], 50, 198)
    assert len(pages) == 1
    assert_coverage(pages, cues, 50)


def test_sparse_transcript_reduces_page_count_instead_of_padding():
    cues = [Cue(index, index + 0.5, f"small{index}") for index in range(79)]
    pages = plan_pages(cues, [], 90, 198)
    assert len(pages) == 1
    assert_coverage(pages, cues, 90)
    assert plan_pages([], [], 90, 198) == []
    assert plan_pages([Cue(1, 2, "  ")], [], 90, 198) == []


def test_large_source_cue_is_never_silently_truncated_or_split():
    cues = [cue(100, 200, "large", 10000)]
    pages = plan_pages(cues, [], 300, 198)
    assert len(pages) == 1
    assert_coverage(pages, cues, 300)
    assert count_words(pages[0].source_text) >= 10000


def test_source_word_counts_are_balanced_when_equal_cues_allow_it():
    cues = [cue(index * 10, index * 10 + 9, f"equal{index}") for index in range(20)]
    pages = plan_pages(cues, [], 200, 5)
    assert all(len(page.source_text.splitlines()) == 4 for page in pages)
    assert_coverage(pages, cues, 200)


def test_prefers_nearby_slide_changes_without_creating_blank_pages():
    cues = [cue(index * 10, index * 10 + 9, f"slide{index}") for index in range(10)]
    pages = plan_pages(cues, [Frame(0, "intro.png"), Frame(49.8, "next.png")], 100, 2)
    assert pages[0].end == pytest.approx(49.8)
    assert pages[0].frame_path == "intro.png"
    assert pages[1].frame_path == "next.png"
    assert_coverage(pages, cues, 100)


def test_representative_visual_uses_dwell_time_not_nearest_future_frame():
    cues = [cue(0, 100, "whole", 400)]
    frames = [Frame(80, "late.png"), Frame(0, "brief.png"), Frame(5, "long.png")]
    pages = plan_pages(cues, frames, 100, 1)
    assert pages[0].frame_path == "long.png"


def test_persistent_slide_can_precede_page_but_future_slide_cannot_be_used():
    cues = [cue(index * 10, index * 10 + 9, f"persistent{index}") for index in range(4)]
    pages = plan_pages(cues, [Frame(0, "persistent.png"), Frame(39, "late.png")], 40, 2)
    assert pages[1].start > 0
    assert pages[1].frame_path == "persistent.png"
    future_only = plan_pages([cue(0, 1, "early"), cue(20, 21, "late")], [Frame(20, "later.png")], 30, 2)
    assert future_only[0].frame_path is None


def test_long_silences_are_covered_without_filler_pages():
    cues = [cue(10, 20, "opening"), cue(900, 920, "ending")]
    pages = plan_pages(cues, [Frame(0, "first.png"), Frame(500, "last.png")], 1000, 2)
    assert len(pages) == 2
    assert_coverage(pages, cues, 1000)
    assert all(page.source_text.strip() for page in pages)


def test_duplicate_frame_timestamps_have_deterministic_persistence():
    pages = plan_pages([cue(0, 9, "same")], [Frame(0, "zero.png"), Frame(0, "actual.png")], 10, 1)
    assert pages[0].frame_path == "actual.png"


@pytest.mark.parametrize("target", [0, -1, 1.5, True])
def test_invalid_page_targets_are_rejected(target):
    with pytest.raises(ValueError, match="positive integer"):
        plan_pages([], [], 10, target)


@pytest.mark.parametrize("duration", [0, -1, math.nan, math.inf])
def test_invalid_video_durations_are_rejected(duration):
    with pytest.raises(ValueError):
        plan_pages([], [], duration, 1)


@pytest.mark.parametrize("bad_cue", [Cue(-1, 2, "bad"), Cue(1, 1, "bad"), Cue(2, 1, "bad"), Cue(1, 11, "bad"), Cue(math.nan, 2, "bad"), Cue(1, math.inf, "bad")])
def test_invalid_source_times_are_rejected(bad_cue):
    with pytest.raises(ValueError):
        plan_pages([bad_cue], [], 10, 1)


@pytest.mark.parametrize("time", [-1, 11, math.nan, math.inf])
def test_invalid_frame_times_are_rejected(time):
    with pytest.raises(ValueError):
        plan_pages([cue(0, 9, "valid")], [Frame(time, "bad.png")], 10, 1)


def test_unicode_reading_units_and_apostrophes():
    assert count_words("The presenter's quick summary.") == 4
    assert count_words("你好世界") == 4
    assert count_words("日本語の要約") == 6
    assert count_words("résumé naïve café") == 3
    assert count_words("  ") == 0
