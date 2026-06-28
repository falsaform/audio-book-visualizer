"""SQLModel tables for the production database (one ``production.db`` per book).

These are the *persistence* schema, distinct from the in-memory pydantic DTOs in
``audiobook_visualizer.models`` (which remain the contract between stages and the
LLM I/O shape). The store translates DTO <-> table.

Design notes:
- The **shot** (:class:`Shot`) is the atomic render unit — one generated image and
  one video cue with its own camera move — grouped under a :class:`ScreenplayScene`.
- A :class:`Segment` carries the per-window time span that the old code derived from
  the chunk *folder name*; the folder still exists (frame images live under it) but is
  no longer the source of truth for timing.
- ``characters_present`` is modelled as association tables (so we can later query
  "which shots feature character X" for continuity); opaque lists (``aliases``,
  frame ``references``) are JSON columns.
- :class:`Location` and :class:`Costume` are defined now but populated by a later
  slice (set/costume design roles).

We avoid ORM ``Relationship`` navigation on purpose: the store reads rows and returns
plain DTOs/views within a session, which sidesteps detached-instance and lazy-load
issues when frames are rendered across a thread pool.
"""

from typing import Optional

from sqlalchemy import JSON, Column, Index, UniqueConstraint
from sqlmodel import Field, SQLModel


class SceneCharacterLink(SQLModel, table=True):
    scene_id: Optional[int] = Field(
        default=None, foreign_key="screenplayscene.id", primary_key=True
    )
    character_id: Optional[int] = Field(
        default=None, foreign_key="character.id", primary_key=True
    )


class ShotCharacterLink(SQLModel, table=True):
    shot_id: Optional[int] = Field(default=None, foreign_key="shot.id", primary_key=True)
    character_id: Optional[int] = Field(
        default=None, foreign_key="character.id", primary_key=True
    )


class Production(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str = ""
    author: str = ""
    source: str = ""  # ebook | audiobook | both
    style: str = ""  # project.style snapshot
    created_at: float = 0.0


class Segment(SQLModel, table=True):
    """A per-window render group — replaces the chunk folder as the timing source."""

    __table_args__ = (UniqueConstraint("production_id", "chunk_label", name="uq_segment_label"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    production_id: int = Field(foreign_key="production.id", index=True)
    chunk_label: str = Field(index=True)  # "0000-60min" | "full"
    start: float = 0.0  # window start (seconds)
    end: float = 0.0  # window end (seconds)
    structure_path: Optional[str] = None


class Character(SQLModel, table=True):
    """The accumulating character bible — replaces ``characters.json``."""

    __table_args__ = (UniqueConstraint("production_id", "name", name="uq_char_name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    production_id: int = Field(foreign_key="production.id", index=True)
    name: str = Field(index=True)
    aliases: list = Field(default_factory=list, sa_column=Column(JSON))
    description: str = ""
    role: str = ""
    portrait_path: Optional[str] = None
    portrait_key: Optional[str] = None  # appearance hash


class ScreenplayScene(SQLModel, table=True):
    """A screenplay unit. In ``scenes``/``paragraphs`` modes this is a *synthetic*
    scene carrying exactly one shot, so the render unit stays uniform."""

    id: Optional[int] = Field(default=None, primary_key=True)
    segment_id: int = Field(foreign_key="segment.id", index=True)
    order_index: int = 0
    slug: str = ""
    chapter: Optional[str] = None
    heading: str = ""  # "INT. HARBOR - DAWN"
    title: str = ""
    summary: str = ""
    setting: str = ""
    time_of_day: str = ""
    mood: str = ""
    action: str = ""  # screenplay prose
    source_excerpt: str = ""
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    revision: int = 0  # bumped by the critic loop
    synthetic: bool = False  # True for legacy-mode rows
    location_id: Optional[int] = Field(default=None, foreign_key="location.id")


class Shot(SQLModel, table=True):
    """THE RENDER UNIT: one generated image + one video cue with its own camera move."""

    __table_args__ = (Index("ix_shot_segment_order", "segment_id", "order_index"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    segment_id: int = Field(foreign_key="segment.id", index=True)
    scene_id: int = Field(foreign_key="screenplayscene.id", index=True)
    order_index: int = 0
    slug: str = Field(default="", index=True)  # == legacy Scene.id; frame filename stem
    shot_type: str = ""
    camera_move: str = "static"
    composition: str = ""
    subject: str = ""
    visual_description: str = ""
    duration_weight: float = 1.0
    start_time: Optional[float] = None  # pinned by Python, never the LLM
    end_time: Optional[float] = None


class Frame(SQLModel, table=True):
    """Replaces ``manifest.json`` and the existence/cached sidecar logic."""

    id: Optional[int] = Field(default=None, primary_key=True)
    shot_id: int = Field(foreign_key="shot.id", unique=True, index=True)
    prompt: str = ""
    image_path: Optional[str] = None  # file on disk; the DB stores the path
    provider: str = ""
    model: str = ""
    references: list = Field(default_factory=list, sa_column=Column(JSON))
    cache_key: Optional[str] = None
    cached: bool = False
    error: Optional[str] = None


class ContinuityNote(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    segment_id: int = Field(foreign_key="segment.id", index=True)
    scene_id: Optional[int] = Field(default=None, foreign_key="screenplayscene.id")
    shot_id: Optional[int] = Field(default=None, foreign_key="shot.id")
    severity: str = "info"  # info | warning | error
    category: str = ""  # appearance | prop | geography | timeline | script
    message: str = ""
    proposed_fix: Optional[str] = None
    status: str = "open"  # open | acknowledged | resolved


# --- designed now, populated by a later slice (set/costume design) ---------


class Location(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    production_id: int = Field(foreign_key="production.id", index=True)
    name: str = ""
    description: str = ""
    reference_path: Optional[str] = None


class Costume(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    production_id: int = Field(foreign_key="production.id", index=True)
    character_id: int = Field(foreign_key="character.id", index=True)
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    description: str = ""
    reference_path: Optional[str] = None
