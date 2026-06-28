"""Ingestion: turn source files (ebook, audiobook) into text and transcripts."""

from .ebook import load_ebook
from .audio import transcribe_audio

__all__ = ["load_ebook", "transcribe_audio"]
