"""Ingestion: turn an audiobook into a transcript and chapter/paragraph structure."""

from .audio import (
    audio_file_boundaries,
    audio_total_duration,
    gather_audio_files,
    transcribe_audio,
)
from .segmentation import build_audiobook_structure, extract_chapter_markers, join_structures

__all__ = [
    "transcribe_audio",
    "gather_audio_files",
    "audio_file_boundaries",
    "audio_total_duration",
    "build_audiobook_structure",
    "extract_chapter_markers",
    "join_structures",
]
