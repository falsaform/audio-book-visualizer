"""Align scenes to audiobook timestamps.

Each scene carries a short ``source_excerpt`` quoted from the narration. We match
it back to the paragraph it came from (in the segmented audiobook structure) and
copy that paragraph's exact start/end times onto the scene.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Optional

from ..models import AudiobookStructure, Paragraph, Scene

_WORD_RE = re.compile(r"\w+")


def _normalize(text: str) -> str:
    return " ".join(_WORD_RE.findall(text.lower()))


def align_scenes_to_structure(
    scenes: list[Scene], structure: AudiobookStructure
) -> None:
    """Attach exact timestamps to scenes from their source paragraph.

    Audiobook-only mode already knows each paragraph's audio span, so instead of
    the fuzzy segment match used for ebooks we match each scene's
    ``source_excerpt`` to the paragraph it came from and copy that paragraph's
    ``start``/``end`` verbatim. Matching is scoped to the scene's chapter when
    known, which is both faster and more accurate.
    """
    by_chapter: dict[str, list[Paragraph]] = {
        ch.title: ch.paragraphs for ch in structure.chapters
    }
    all_paragraphs = [p for ch in structure.chapters for p in ch.paragraphs]
    norm_cache: dict[int, str] = {id(p): _normalize(p.text) for p in all_paragraphs}

    for scene in scenes:
        needle = _normalize(scene.source_excerpt)
        if len(needle) < 12:
            continue
        candidates = by_chapter.get(scene.chapter or "", None) or all_paragraphs
        match = _best_paragraph(needle, candidates, norm_cache)
        if match is not None:
            scene.start_time = match.start
            scene.end_time = match.end


def _best_paragraph(
    needle: str, paragraphs: list[Paragraph], norm_cache: dict[int, str]
) -> Optional[Paragraph]:
    head = needle[:120]
    best: Optional[Paragraph] = None
    best_score = 0.0
    for para in paragraphs:
        norm = norm_cache.get(id(para)) or _normalize(para.text)
        if not norm:
            continue
        if head in norm:  # excerpt is a verbatim slice of this paragraph
            return para
        score = SequenceMatcher(None, head, norm).ratio()
        if score > best_score:
            best_score = score
            best = para
    return best if best_score >= 0.35 else None
