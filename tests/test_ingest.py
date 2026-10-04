import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from bookagent.ingest import _choose_subtitle, fetch_youtube, load_transcript, probe_video


def test_srt_preserves_timestamps_and_strips_markup(tmp_path):
    path = tmp_path / "captions.srt"
    path.write_text("1\n00:00:01,250 --> 00:00:03,500\n<b>Hello</b> &amp; welcome.\nSecond line.\n", encoding="utf-8")
    cues = load_transcript(path)
    assert [(cue.start, cue.end, cue.text) for cue in cues] == [(1.25, 3.5, "Hello & welcome. Second line.")]


def test_vtt_deduplicates_rolling_windows_using_original_context(tmp_path):
    path = tmp_path / "captions.vtt"
    path.write_text(
        "WEBVTT\n\n"
        "00:00.000 --> 00:02.000\nhello and welcome\n\n"
        "00:01.000 --> 00:03.000 align:start\n<c>hello and welcome</c> <00:01.500>to class\n\n"
        "00:02.000 --> 00:04.000\nto class today\n\n"
        "00:06.000 --> 00:07.000\nhello and welcome\n",
        encoding="utf-8",
    )
    cues = load_transcript(path)
    assert [cue.text for cue in cues] == ["hello and welcome", "to class", "today", "hello and welcome"]
    assert [cue.start for cue in cues] == [0, 1, 2, 6]


def test_exact_rolling_duplicate_extends_existing_window(tmp_path):
    path = tmp_path / "captions.vtt"
    path.write_text("WEBVTT\n\n00:00.000 --> 00:01.000\n<00:00.000>a repeated caption\n\n00:01.000 --> 00:01.010\na repeated caption\n", encoding="utf-8")
    cues = load_transcript(path)
    assert len(cues) == 1
    assert cues[0].end == 1.01


def test_youtube_leading_whitespace_and_empty_cues(tmp_path):
    path = tmp_path / "captions.vtt"
    path.write_text("WEBVTT\nKind: captions\nLanguage: en\n\n00:00.000 --> 00:01.000\n \nand<00:00.250><c> welcome.</c>\n\n00:01.000 --> 00:01.010\n \n\n00:01.010 --> 00:03.000\nand welcome.\nWe<00:01.250><c> begin.</c>\n", encoding="utf-8")
    cues = load_transcript(path)
    assert [cue.text for cue in cues] == ["and welcome.", "We begin."]
    assert [cue.start for cue in cues] == [0, 1.01]


@pytest.mark.parametrize("suffix", ["srt", "vtt", "json"])
def test_intentional_repeated_speech_is_preserved(tmp_path, suffix):
    path = tmp_path / f"captions.{suffix}"
    if suffix == "json":
        content = json.dumps([{"start": 0, "end": 1, "text": "Go ahead"}, {"start": 1, "end": 2, "text": "Go ahead"}])
    else:
        content = "00:00:00.000 --> 00:00:01.000\nGo ahead\n\n00:00:01.000 --> 00:00:02.000\nGo ahead\n"
    path.write_text(content, encoding="utf-8")
    assert [cue.text for cue in load_transcript(path)] == ["Go ahead", "Go ahead"]


def test_empty_subtitle_cues_still_validate_timestamps(tmp_path):
    path = tmp_path / "captions.vtt"
    path.write_text("WEBVTT\n\n00:01.000 --> 00:00.000\n \n\n00:02.000 --> 00:03.000\nhello\n", encoding="utf-8")
    with pytest.raises(ValueError, match="end > start"):
        load_transcript(path)


def test_empty_srt_cue_does_not_turn_next_index_into_caption(tmp_path):
    path = tmp_path / "captions.srt"
    path.write_text("1\n00:00:00,000 --> 00:00:01,000\n \n\n2\n00:00:01,000 --> 00:00:02,000\nhello\n", encoding="utf-8")
    assert [(cue.start, cue.text) for cue in load_transcript(path)] == [(1, "hello")]


def test_webvtt_note_and_style_blocks_are_metadata(tmp_path):
    path = tmp_path / "captions.vtt"
    path.write_text("WEBVTT\n\nNOTE an explanation\nAn arrow --> inside metadata\n\nSTYLE\n::cue {color:white;}\n\n00:00.000 --> 00:01.000\nhello\n", encoding="utf-8")
    assert [cue.text for cue in load_transcript(path)] == ["hello"]


@pytest.mark.parametrize("wrapped", [False, True])
def test_json_list_and_whisper_segments(tmp_path, wrapped):
    path = tmp_path / "captions.json"
    segments = [{"start": 1.25, "duration": 2, "text": "One point"}, {"start": 4, "end": 5, "text": "Another point"}]
    path.write_text(json.dumps({"segments": segments} if wrapped else segments), encoding="utf-8")
    assert [(cue.start, cue.end) for cue in load_transcript(path)] == [(1.25, 3.25), (4, 5)]


@pytest.mark.parametrize("entry", [
    {"start": -1, "end": 2, "text": "bad"},
    {"start": 1, "end": 1, "text": "bad"},
    {"start": float("nan"), "end": 2, "text": "bad"},
    {"start": 0, "end": float("inf"), "text": "bad"},
    {"start": False, "end": 2, "text": "bad"},
    {"start": 0, "end": 2, "text": "<b></b>"},
    {"text": "no timestamps"},
])
def test_invalid_json_cues_rejected(tmp_path, entry):
    path = tmp_path / "captions.json"
    path.write_text(json.dumps([entry]), encoding="utf-8")
    with pytest.raises(ValueError):
        load_transcript(path)


def test_plain_text_never_gets_invented_timestamps(tmp_path):
    path = tmp_path / "captions.txt"
    path.write_text("Just spoken text.", encoding="utf-8")
    with pytest.raises(ValueError, match="plain text cannot be aligned"):
        load_transcript(path)


def test_subtitle_selection_prefers_manual_and_matching_variant():
    info = {"subtitles": {"en-US": [1], "fr": [1]}, "automatic_captions": {"en": [1]}}
    assert _choose_subtitle(info, "en") == ("en-US", False)
    assert _choose_subtitle(info, "de") == (None, False)
    info["subtitles"]["en"] = [1]
    assert _choose_subtitle(info, "en") == ("en", False)


def test_youtube_downloads_one_manual_caption_without_video_or_cookies(tmp_path, monkeypatch):
    options_seen = []
    metadata = {"title": "Example", "duration": 240, "webpage_url": "https://www.youtube.com/watch?v=example", "subtitles": {"en-US": [{}]}, "automatic_captions": {"en": [{}]}}

    class Downloader:
        def __init__(self, options):
            self.options = options
            options_seen.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def extract_info(self, url, download):
            if download:
                path = tmp_path / "source.en-US.vtt"
                path.write_text("WEBVTT\n\n00:00.000 --> 00:01.000\nhello\n", encoding="utf-8")
                return {**metadata, "requested_subtitles": {"en-US": {"filepath": str(path)}}}
            return metadata

    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=Downloader))
    result = fetch_youtube("https://www.youtube.com/watch?v=example&list=abc", tmp_path, download_video=False)
    assert result["title"] == "Example"
    assert Path(result["transcript_path"]).is_file()
    assert result["video_path"] is None
    assert len(options_seen) == 2
    assert all(options["noplaylist"] for options in options_seen)
    assert options_seen[-1]["writesubtitles"] is True
    assert options_seen[-1]["writeautomaticsub"] is False
    assert not any("cookiesfrombrowser" in options or "cookiefile" in options for options in options_seen)


def test_youtube_block_has_local_input_guidance(tmp_path, monkeypatch):
    class Downloader:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def extract_info(self, *args, **kwargs):
            raise RuntimeError("Sign in to confirm you are not a bot")

    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=Downloader))
    with pytest.raises(RuntimeError, match="local video and timestamped"):
        fetch_youtube("https://www.youtube.com/watch?v=example", tmp_path)


def test_probe_duration_and_invalid_duration(tmp_path, monkeypatch):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"placeholder")
    monkeypatch.setattr("bookagent.ingest.subprocess.run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, '{"format":{"duration":"245.125"}}', ""))
    assert probe_video(path) == 245.125
    monkeypatch.setattr("bookagent.ingest.subprocess.run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, '{"format":{"duration":"nan"}}', ""))
    with pytest.raises(RuntimeError, match="finite and positive"):
        probe_video(path)
