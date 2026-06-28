"""Ingestion: turn source files (ebook, audiobook) into text and transcripts."""

from .ebook import load_ebook
from .audio import transcribe_audio
from .segmentation import build_audiobook_structure, extract_chapter_markers, join_structures

__all__ = [
    "load_ebook",
    "transcribe_audio",
    "build_audiobook_structure",
    "extract_chapter_markers",
    "join_structures",
]
