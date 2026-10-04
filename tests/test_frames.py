import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from bookagent.frames import _select_frames, extract_frames
from bookagent.models import Frame


def _frame(tmp_path, timestamp, color, text=False):
    image = Image.new("RGB", (256, 144), color)
    if text:
        draw = ImageDraw.Draw(image)
        for row in range(20, 120, 10):
            draw.line((15, row, 235, row), fill="black", width=2)
    path = tmp_path / f"image-{timestamp}.jpg"
    image.save(path)
    return Frame(timestamp, str(path))


def test_selection_keeps_first_consecutive_changes_and_scene_onset(tmp_path):
    frames = [
        _frame(tmp_path, 0, "black"),
        _frame(tmp_path, 12, "black"),
        _frame(tmp_path, 24, "white"),
        _frame(tmp_path, 36, "white", text=True),
        _frame(tmp_path, 48, "black"),
    ]
    # White frames and added text each preserve their earliest detected onset.
    # A later return to black remains a separate change.
    selected = _select_frames(frames, threshold=0.2)
    assert [frame.timestamp for frame in selected] == [0, 24, 36, 48]


def test_white_slide_text_changes_detected_and_compression_noise_ignored(tmp_path):
    first = Image.new("RGB", (512, 288), "white")
    draw = ImageDraw.Draw(first)
    # Small compared with the page's white area, large enough to be slide text.
    draw.rectangle((40, 60, 250, 72), fill="black")
    first_path = tmp_path / "slide-one.jpg"
    first.save(first_path, quality=95)
    duplicate_path = tmp_path / "slide-one-recompressed.jpg"
    with Image.open(first_path) as duplicate:
        duplicate.save(duplicate_path, quality=85)
    second = Image.new("RGB", (512, 288), "white")
    draw = ImageDraw.Draw(second)
    draw.rectangle((40, 100, 300, 112), fill="black")
    second_path = tmp_path / "slide-two.jpg"
    second.save(second_path, quality=95)
    selected = _select_frames([Frame(0, str(first_path)), Frame(12, str(duplicate_path)), Frame(24, str(second_path))], threshold=0.12)
    assert [frame.timestamp for frame in selected] == [0, 24]


def test_one_ffmpeg_pass_source_timestamps_and_cache_invalidation(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"placeholder video")
    output = tmp_path / "frames"
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        template = command[-1]
        for index, color in enumerate(("black", "white", "white"), 1):
            Image.new("RGB", (100, 60), color).save(template.replace("%06d", f"{index:06d}"))
        return subprocess.CompletedProcess(command, 0, "", "[Parsed_showinfo] n: 0 pts: 0 pts_time:0\n[Parsed_showinfo] n: 1 pts: 12345 pts_time:12.345\n[Parsed_showinfo] n: 2 pts: 24701 pts_time:24.701\n")

    monkeypatch.setattr("bookagent.frames.subprocess.run", run)
    first = extract_frames(video, output, interval=12)
    assert len(calls) == 1
    assert calls[0][0] == "ffmpeg"
    assert [frame.timestamp for frame in first] == [0, 12.345]
    assert extract_frames(video, output, interval=12) == first
    assert len(calls) == 1
    extract_frames(video, output, interval=6)
    assert len(calls) == 2
    video.write_bytes(b"changed source video")
    extract_frames(video, output, interval=12)
    assert len(calls) == 3


def test_ffmpeg_timestamp_mismatch_does_not_create_cache(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"placeholder")
    output = tmp_path / "frames"

    def run(command, **kwargs):
        Image.new("RGB", (100, 60), "white").save(command[-1].replace("%06d", "000001"))
        return subprocess.CompletedProcess(command, 0, "", "no showinfo timestamps")

    monkeypatch.setattr("bookagent.frames.subprocess.run", run)
    with pytest.raises(RuntimeError, match="refusing to invent"):
        extract_frames(video, output)
    assert not list(output.glob("frames-*.json"))


@pytest.mark.parametrize("options", [
    {"interval": 0}, {"interval": float("inf")},
    {"threshold": -0.1}, {"threshold": 1.1},
    {"crop": (0.8, 0, 0.3, 1)}, {"crop": (0, 0, 0, 1)},
    {"crop": (0, 0, float("nan"), 1)},
])
def test_invalid_sampling_options_rejected_before_ffmpeg(tmp_path, options):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"placeholder")
    with pytest.raises(ValueError):
        extract_frames(video, tmp_path / "frames", **options)


def test_ffmpeg_failure_reports_error_without_caching(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"placeholder")
    monkeypatch.setattr("bookagent.frames.subprocess.run", lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", "Invalid data found"))
    output = tmp_path / "frames"
    with pytest.raises(RuntimeError, match="Invalid data found"):
        extract_frames(video, output)
    assert not list(output.glob("frames-*.json"))
