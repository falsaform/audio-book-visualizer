"""Ingestion: turn source files (ebook, audiobook) into text and transcripts."""

from .ebook import load_ebook
from .audio import audio_file_boundaries, gather_audio_files, transcribe_audio
from .segmentation import build_audiobook_structure, extract_chapter_markers, join_structures

__all__ = [
    "load_ebook",
    "transcribe_audio",
    "gather_audio_files",
    "audio_file_boundaries",
    "build_audiobook_structure",
    "extract_chapter_markers",
    "join_structures",
]
