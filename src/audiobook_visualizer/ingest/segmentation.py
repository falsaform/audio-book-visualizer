"""Segment a transcribed audiobook into chapters and paragraphs.

Audiobook-only mode has no ebook to lean on for structure, so we recover it from
the audio + transcript. Chapter boundaries come from, in order of preference:

1. **Embedded markers** — many ``.m4b`` files carry a chapter table; we read it
   with ``ffprobe`` (ffmpeg is already in the image).
2. **Spoken headings** — the narrator says "Chapter One" etc.; we detect those
   at the start of transcript segments.
3. **Time windows** — fixed-length fallback so even an unstructured recording is
   broken into manageable, individually-processable pieces.

Within each chapter, paragraphs are recovered from the narration's natural
pauses (gaps between transcript segments) landing on sentence boundaries.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from ..config import AudioConfig
from ..models import AudioChapter, AudiobookStructure, Paragraph, Transcript, TranscriptSegment

_SENTENCE_END = ('.', '!', '?', '"', '”', '’', ')')
_SPOKEN_CHAPTER_RE = re.compile(
    r"^\s*(chapter\s+[\w-]+|part\s+[\w-]+|book\s+[\w-]+|prologue|epilogue)\b",
    re.IGNORECASE,
)


# -- joining sub-segments ----------------------------------------------------


def join_structures(
    structures: list[AudiobookStructure], title: Optional[str] = None
) -> AudiobookStructure:
    """Merge sub-segment structures (time windows of one book) into one.

    Because preview windows keep absolute timestamps, we order all chapters by
    start time and concatenate them. Consecutive chapters with the same title
    (a chapter split across a window boundary) are stitched back together. Use
    non-overlapping windows to avoid duplicated content.
    """
    chapters: list[AudioChapter] = []
    for st in structures:
        chapters.extend(st.chapters)
    chapters.sort(key=lambda c: c.start if c.start is not None else 0.0)

    merged: list[AudioChapter] = []
    for ch in chapters:
        if merged and merged[-1].title.strip().lower() == ch.title.strip().lower():
            merged[-1].paragraphs.extend(p.model_copy(deep=True) for p in ch.paragraphs)
            if ch.end is not None:
                merged[-1].end = ch.end
        else:
            merged.append(ch.model_copy(deep=True))

    for i, ch in enumerate(merged):
        ch.index = i
        for j, para in enumerate(ch.paragraphs):
            para.index = j

    src_title = structures[0].title if structures else ""
    return AudiobookStructure(
        title=title or src_title, source="joined", chapters=merged
    )


# -- public entry point -----------------------------------------------------


def build_audiobook_structure(
    transcript: Transcript,
    audio_path: str | Path,
    config: AudioConfig,
    title: str = "",
) -> AudiobookStructure:
    """Segment ``transcript`` into chapters and paragraphs per ``config``."""
    mode = (config.chapter_mode or "auto").lower()

    chapters: Optional[list[AudioChapter]] = None
    source = "single"

    if mode in ("auto", "markers"):
        markers = extract_chapter_markers(audio_path)
        if markers:
            chapters = _chapters_from_markers(transcript, markers, config)
            source = "markers"

    if chapters is None and mode in ("auto", "headings"):
        from_headings = _chapters_from_headings(transcript, config)
        # Only trust heading detection if it actually found multiple chapters.
        if from_headings and len(from_headings) > 1:
            chapters = from_headings
            source = "headings"

    if chapters is None and mode in ("auto", "time"):
        chapters = _chapters_by_time(transcript, config)
        source = "time"

    if chapters is None:  # mode == "markers"/"headings" but none were found
        chapters = [_single_chapter(transcript, config, title)]
        source = "single"

    return AudiobookStructure(title=title, source=source, chapters=chapters)


# -- chapter markers (ffprobe) ----------------------------------------------


def extract_chapter_markers(
    audio_path: str | Path,
) -> Optional[list[tuple[str, float, float]]]:
    """Read embedded chapter markers via ffprobe. Returns ``None`` if absent."""
    if not shutil.which("ffprobe"):
        return None
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-i", str(audio_path),
                "-show_chapters", "-print_format", "json", "-loglevel", "error",
            ],
            capture_output=True, text=True, timeout=120,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None

    markers: list[tuple[str, float, float]] = []
    for ch in data.get("chapters", []):
        try:
            start = float(ch.get("start_time", 0.0))
            end = float(ch.get("end_time", 0.0))
        except (TypeError, ValueError):
            continue
        title = (ch.get("tags") or {}).get("title") or f"Chapter {len(markers) + 1}"
        markers.append((title.strip(), start, end))
    return markers or None


def _chapters_from_markers(
    transcript: Transcript,
    markers: list[tuple[str, float, float]],
    config: AudioConfig,
) -> list[AudioChapter]:
    chapters: list[AudioChapter] = []
    for i, (title, start, end) in enumerate(markers):
        segs = [s for s in transcript.segments if start <= s.start < end] if end > start \
            else [s for s in transcript.segments if s.start >= start]
        paragraphs = _segment_into_paragraphs(segs, config)
        if not paragraphs:
            continue
        chapters.append(
            AudioChapter(
                index=i,
                title=title or f"Chapter {i + 1}",
                start=start,
                end=end or (segs[-1].end if segs else None),
                paragraphs=paragraphs,
            )
        )
    return chapters


# -- spoken-heading detection -----------------------------------------------


def _chapters_from_headings(
    transcript: Transcript, config: AudioConfig
) -> list[AudioChapter]:
    """Split the transcript where a segment begins with a spoken chapter heading.

    The heading words themselves are stripped from the prose (so paragraph text
    and timestamps reflect the narration, not "Chapter Two."), but the moment the
    heading is spoken is kept as the chapter's start time.
    """
    groups: list[dict] = []
    current: Optional[dict] = None

    for seg in transcript.segments:
        text = seg.text.strip()
        match = _SPOKEN_CHAPTER_RE.match(text)
        if match:
            if current:
                groups.append(current)
            heading = match.group(0)
            remainder = text[len(heading):].lstrip(" .—-:;,").strip()
            current = {"title": _clean_heading(heading), "start": seg.start, "segs": []}
            if remainder:  # prose shared the heading's segment — keep it
                current["segs"].append(
                    TranscriptSegment(start=seg.start, end=seg.end, text=remainder)
                )
        else:
            if current is None:  # narration before the first heading
                current = {"title": "Opening", "start": seg.start, "segs": []}
            current["segs"].append(seg)
    if current:
        groups.append(current)

    chapters: list[AudioChapter] = []
    for i, group in enumerate(groups):
        paragraphs = _segment_into_paragraphs(group["segs"], config)
        if not paragraphs:
            continue
        chapters.append(
            AudioChapter(
                index=i,
                title=group["title"],
                start=group["start"],
                end=group["segs"][-1].end if group["segs"] else group["start"],
                paragraphs=paragraphs,
            )
        )
    return chapters


def _clean_heading(text: str) -> str:
    return " ".join(w.capitalize() for w in text.strip().split())


# -- time-window fallback ----------------------------------------------------


def _chapters_by_time(transcript: Transcript, config: AudioConfig) -> list[AudioChapter]:
    window = max(60.0, float(config.chapter_seconds))
    if not transcript.segments:
        return []
    total_end = transcript.segments[-1].end
    chapters: list[AudioChapter] = []
    i = 0
    start = 0.0
    while start < total_end:
        end = start + window
        segs = [s for s in transcript.segments if start <= s.start < end]
        paragraphs = _segment_into_paragraphs(segs, config)
        if paragraphs:
            chapters.append(
                AudioChapter(
                    index=i,
                    title=f"Part {i + 1} ({_fmt(start)}–{_fmt(min(end, total_end))})",
                    start=start,
                    end=min(end, total_end),
                    paragraphs=paragraphs,
                )
            )
            i += 1
        start = end
    return chapters


def _single_chapter(
    transcript: Transcript, config: AudioConfig, title: str
) -> AudioChapter:
    paragraphs = _segment_into_paragraphs(transcript.segments, config)
    return AudioChapter(
        index=0,
        title=title or "Audiobook",
        start=transcript.segments[0].start if transcript.segments else None,
        end=transcript.segments[-1].end if transcript.segments else None,
        paragraphs=paragraphs,
    )


# -- paragraph segmentation --------------------------------------------------


def _segment_into_paragraphs(
    segments: list[TranscriptSegment], config: AudioConfig
) -> list[Paragraph]:
    """Group consecutive segments into paragraphs on sentence-ending pauses."""
    gap_threshold = float(config.paragraph_gap)
    max_chars = int(config.paragraph_max_chars)

    paragraphs: list[Paragraph] = []
    buf: list[str] = []
    buf_start: Optional[float] = None
    buf_end: Optional[float] = None
    prev_end: Optional[float] = None

    def flush() -> None:
        nonlocal buf, buf_start, buf_end
        text = " ".join(t.strip() for t in buf).strip()
        if text:
            paragraphs.append(
                Paragraph(index=len(paragraphs), text=text, start=buf_start, end=buf_end)
            )
        buf, buf_start, buf_end = [], None, None

    for seg in segments:
        gap = (seg.start - prev_end) if prev_end is not None else 0.0
        cur_len = sum(len(t) for t in buf)
        ends_sentence = bool(buf) and buf[-1].rstrip().endswith(_SENTENCE_END)
        if buf and ((gap >= gap_threshold and ends_sentence) or cur_len >= max_chars):
            flush()
        if buf_start is None:
            buf_start = seg.start
        buf.append(seg.text)
        buf_end = seg.end
        prev_end = seg.end

    flush()
    return paragraphs


# -- helpers -----------------------------------------------------------------


def _fmt(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"
