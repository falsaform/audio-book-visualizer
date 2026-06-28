"""Align scenes to audiobook timestamps.

Forced alignment between an ebook and its narration is a hard problem. For the
MVP we use a pragmatic approach: each scene carries a short ``source_excerpt``
quoted from the book. We fuzzy-match that excerpt against the rolling text of
the transcript segments and attach the timestamp of the best match. Good enough
to scrub a gallery in sync with playback; not a substitute for true forced
alignment.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from ..models import Scene, Transcript

_WORD_RE = re.compile(r"\w+")


def _normalize(text: str) -> str:
    return " ".join(_WORD_RE.findall(text.lower()))


def align_scenes(scenes: list[Scene], transcript: Transcript) -> None:
    """Attach ``start_time``/``end_time`` to scenes in place (best effort)."""
    if not transcript.segments:
        return

    seg_texts = [_normalize(seg.text) for seg in transcript.segments]
    # Precompute cumulative text so we can match excerpts spanning segments.
    for scene in scenes:
        needle = _normalize(scene.source_excerpt)
        if len(needle) < 12:  # too short to match reliably
            continue
        best_idx, best_score = _best_segment(needle, seg_texts)
        if best_idx is not None and best_score >= 0.45:
            scene.start_time = transcript.segments[best_idx].start
            scene.end_time = transcript.segments[best_idx].end


def _best_segment(needle: str, seg_texts: list[str]) -> tuple[int | None, float]:
    """Return the index of the transcript segment that best contains ``needle``."""
    needle_head = needle[:80]
    best_idx: int | None = None
    best_score = 0.0
    # Compare against a sliding window of 3 segments joined, to tolerate the
    # excerpt straddling segment boundaries.
    for i in range(len(seg_texts)):
        window = " ".join(seg_texts[i : i + 3])
        if not window:
            continue
        score = SequenceMatcher(None, needle_head, window[: len(needle_head) + 40]).ratio()
        if score > best_score:
            best_score = score
            best_idx = i
    return best_idx, best_score
