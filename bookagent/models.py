"""Shared, serializable pipeline data."""

from dataclasses import dataclass, field


@dataclass
class Cue:
    start: float
    end: float
    text: str


@dataclass
class Frame:
    timestamp: float
    path: str


@dataclass
class Page:
    number: int
    title: str
    start: float
    end: float
    summary: str
    bullets: list[str]
    takeaway: str
    frame_path: str | None = None
    source_text: str = ""
    kind: str = "content"
    chapter: str = ""
    uncertainty: str = ""


@dataclass
class Book:
    title: str
    source_url: str
    duration: float
    pages: list[Page]
    reading_minutes: float = 60.0
    warnings: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def timestamp(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"
