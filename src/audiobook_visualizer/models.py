"""Pydantic data models shared across the pipeline.

These are the contract between stages: ingestion produces raw text and an
optional transcript, analysis turns that into ``Character`` and ``Scene``
records, and generation turns scenes into ``Frame`` records.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class Character(BaseModel):
    """A character with a canonical visual description used for consistency.

    The ``description`` is intentionally appearance-focused (not personality):
    it is injected verbatim into image prompts so the same character looks the
    same across every generated frame.
    """

    name: str
    aliases: list[str] = Field(default_factory=list)
    description: str = ""
    role: str = ""

    def matches(self, mention: str) -> bool:
        mention = mention.strip().lower()
        if mention == self.name.strip().lower():
            return True
        return any(mention == alias.strip().lower() for alias in self.aliases)


class TranscriptSegment(BaseModel):
    """One timestamped chunk of transcribed audio."""

    start: float
    end: float
    text: str


class Transcript(BaseModel):
    language: Optional[str] = None
    segments: list[TranscriptSegment] = Field(default_factory=list)

    @property
    def full_text(self) -> str:
        return " ".join(seg.text.strip() for seg in self.segments)


class Scene(BaseModel):
    """A single visual moment worth rendering as a still frame."""

    id: str
    chapter: Optional[str] = None
    title: str = ""
    summary: str = ""
    setting: str = ""
    time_of_day: str = ""
    mood: str = ""
    characters_present: list[str] = Field(default_factory=list)
    visual_description: str = ""
    source_excerpt: str = ""
    # Optional audio timing, filled in when an aligned transcript is available.
    start_time: Optional[float] = None
    end_time: Optional[float] = None


class Frame(BaseModel):
    """The rendered output for one scene."""

    scene_id: str
    prompt: str
    image_path: Optional[str] = None
    provider: str = ""
    model: str = ""
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.image_path is not None and self.error is None


class BookAnalysis(BaseModel):
    """The full analyzed representation of a book."""

    title: str = ""
    author: str = ""
    characters: list[Character] = Field(default_factory=list)
    scenes: list[Scene] = Field(default_factory=list)

    def character(self, mention: str) -> Optional[Character]:
        for char in self.characters:
            if char.matches(mention):
                return char
        return None
