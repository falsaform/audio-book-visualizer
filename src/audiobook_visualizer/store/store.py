"""The production data store: a thin, DTO-returning facade over the SQLModel tables.

One ``production.db`` lives at ``<book_dir>/production.db``. The store owns all
session handling and returns plain pydantic DTOs / view dataclasses (never live ORM
rows), so callers — including the thread-pooled frame renderer — never touch a
session or a detached instance.

Writes are guarded by an instance lock (plus SQLite WAL + ``busy_timeout``) so the
parallel renderer can persist frames without "database is locked" errors.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, delete, select

from ..models import BookAnalysis, Character as CharacterDTO, Frame as FrameDTO, Scene
from .models import (
    Character,
    ContinuityNote,
    Frame,
    Production,
    RenderJob,
    SceneCharacterLink,
    ScreenplayScene,
    Segment,
    Shot,
    ShotCharacterLink,
)

# -- read views (plain data; safe to use outside a session) ------------------


@dataclass
class ShotView:
    slug: str
    order_index: int
    scene_slug: str
    chapter: Optional[str]
    heading: str
    title: str
    summary: str
    setting: str
    time_of_day: str
    mood: str
    shot_type: str
    camera_move: str
    composition: str
    subject: str
    visual_description: str
    source_excerpt: str
    characters_present: list[str]
    start_time: Optional[float]
    end_time: Optional[float]
    frame: Optional[FrameDTO]

    def to_scene(self) -> Scene:
        """Reconstruct the legacy :class:`Scene` DTO (the render-unit contract)."""
        return Scene(
            id=self.slug,
            chapter=self.chapter,
            title=self.title,
            summary=self.summary,
            setting=self.setting,
            time_of_day=self.time_of_day,
            mood=self.mood,
            shot_type=self.shot_type,
            characters_present=list(self.characters_present),
            visual_description=self.visual_description,
            source_excerpt=self.source_excerpt,
            start_time=self.start_time,
            end_time=self.end_time,
        )


@dataclass
class SegmentView:
    chunk_label: str
    start: float
    end: float
    shots: list[ShotView] = field(default_factory=list)


@dataclass
class JobView:
    id: int
    shot_slug: str
    status: str
    error: Optional[str]
    image_path: Optional[str]
    created_at: float
    updated_at: float


@dataclass
class ProductionView:
    title: str
    author: str
    characters: list[CharacterDTO]
    segments: list[SegmentView]

    @property
    def shots(self) -> list[ShotView]:
        return [s for seg in self.segments for s in seg.shots]


def _norm(name: str) -> str:
    return name.strip().lower()


class ProductionStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False},
        )
        _enable_sqlite_pragmas(self._engine)
        self._write_lock = threading.Lock()
        self.create_all()

    @classmethod
    def open(cls, book_dir: str | Path) -> "ProductionStore":
        return cls(Path(book_dir) / "production.db")

    def create_all(self) -> None:
        SQLModel.metadata.create_all(self._engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        with Session(self._engine) as session:
            yield session

    # -- production / segment -----------------------------------------------

    def get_or_create_production(
        self, title: str = "", author: str = "", source: str = "", style: str = ""
    ) -> int:
        with self._write_lock, self.session() as s:
            prod = s.exec(select(Production)).first()
            if prod is None:
                prod = Production(title=title, author=author, source=source, style=style)
                s.add(prod)
            else:
                # Backfill metadata that was unknown on an earlier run.
                prod.title = prod.title or title
                prod.author = prod.author or author
                prod.source = prod.source or source
                prod.style = prod.style or style
                s.add(prod)
            s.commit()
            return int(prod.id)

    def get_production_id(self) -> Optional[int]:
        with self.session() as s:
            prod = s.exec(select(Production)).first()
            return int(prod.id) if prod else None

    def get_or_create_segment(
        self,
        production_id: int,
        chunk_label: str,
        start: float,
        end: float,
        structure_path: Optional[str] = None,
    ) -> int:
        with self._write_lock, self.session() as s:
            seg = s.exec(
                select(Segment).where(
                    Segment.production_id == production_id,
                    Segment.chunk_label == chunk_label,
                )
            ).first()
            if seg is None:
                seg = Segment(
                    production_id=production_id, chunk_label=chunk_label,
                    start=start, end=end, structure_path=structure_path,
                )
            else:
                seg.start, seg.end = start, end
                if structure_path:
                    seg.structure_path = structure_path
            s.add(seg)
            s.commit()
            return int(seg.id)

    def list_segments(self, production_id: int) -> list[tuple[int, str, float, float]]:
        """Return ``(id, chunk_label, start, end)`` per segment, ordered by start."""
        with self.session() as s:
            rows = s.exec(
                select(Segment).where(Segment.production_id == production_id)
            ).all()
        rows.sort(key=lambda r: (r.start, r.chunk_label))
        return [(int(r.id), r.chunk_label, r.start, r.end) for r in rows]

    def find_segment(self, production_id: int, chunk_label: str) -> Optional[int]:
        with self.session() as s:
            seg = s.exec(
                select(Segment).where(
                    Segment.production_id == production_id,
                    Segment.chunk_label == chunk_label,
                )
            ).first()
            return int(seg.id) if seg else None

    # -- characters (the bible) ---------------------------------------------

    def upsert_characters(
        self, production_id: int, chars: list[CharacterDTO]
    ) -> list[CharacterDTO]:
        """Merge by case-insensitive name: union aliases, prefer richer description."""
        with self._write_lock, self.session() as s:
            existing = {
                _norm(c.name): c
                for c in s.exec(
                    select(Character).where(Character.production_id == production_id)
                ).all()
            }
            for dto in chars:
                key = _norm(dto.name)
                row = existing.get(key)
                if row is None:
                    row = Character(
                        production_id=production_id, name=dto.name,
                        aliases=list(dto.aliases), description=dto.description, role=dto.role,
                    )
                    existing[key] = row
                else:
                    row.aliases = sorted({*row.aliases, *dto.aliases})
                    if len(dto.description) > len(row.description):
                        row.description = dto.description
                    row.role = row.role or dto.role
                s.add(row)
            s.commit()
        return self.list_characters(production_id)

    def list_characters(self, production_id: int) -> list[CharacterDTO]:
        with self.session() as s:
            rows = s.exec(
                select(Character).where(Character.production_id == production_id)
            ).all()
        return [
            CharacterDTO(
                name=r.name, aliases=list(r.aliases), description=r.description, role=r.role
            )
            for r in rows
        ]

    def set_portrait(self, production_id: int, name: str, path: str, key: str) -> None:
        with self._write_lock, self.session() as s:
            row = s.exec(
                select(Character).where(
                    Character.production_id == production_id, Character.name == name
                )
            ).first()
            if row is not None:
                row.portrait_path, row.portrait_key = path, key
                s.add(row)
                s.commit()

    def get_portraits(self, production_id: int) -> dict[str, str]:
        """Map canonical name + each alias (lowercased) -> portrait path."""
        out: dict[str, str] = {}
        with self.session() as s:
            rows = s.exec(
                select(Character).where(Character.production_id == production_id)
            ).all()
            for r in rows:
                if r.portrait_path:
                    out[_norm(r.name)] = r.portrait_path
                    for a in r.aliases:
                        out[_norm(a)] = r.portrait_path
        return out

    # -- scenes / shots -----------------------------------------------------

    def clear_segment(self, segment_id: int) -> None:
        """Drop all scenes/shots/frames/links/notes for a segment (used on reanalyze)."""
        with self._write_lock, self.session() as s:
            scene_ids = [r for r in s.exec(
                select(ScreenplayScene.id).where(ScreenplayScene.segment_id == segment_id)
            ).all()]
            shot_ids = [r for r in s.exec(
                select(Shot.id).where(Shot.segment_id == segment_id)
            ).all()]
            if shot_ids:
                s.exec(delete(Frame).where(Frame.shot_id.in_(shot_ids)))
                s.exec(delete(ShotCharacterLink).where(ShotCharacterLink.shot_id.in_(shot_ids)))
            if scene_ids:
                s.exec(delete(SceneCharacterLink).where(SceneCharacterLink.scene_id.in_(scene_ids)))
            s.exec(delete(Shot).where(Shot.segment_id == segment_id))
            s.exec(delete(ScreenplayScene).where(ScreenplayScene.segment_id == segment_id))
            s.exec(delete(ContinuityNote).where(ContinuityNote.segment_id == segment_id))
            s.commit()

    def persist_scenes_as_shots(
        self, production_id: int, segment_id: int, scenes: list[Scene]
    ) -> None:
        """Legacy mapping: each :class:`Scene` DTO -> one synthetic screenplay scene
        carrying exactly one shot (``camera_move='static'``). Replaces the segment's
        prior content so reruns don't duplicate."""
        self.clear_segment(segment_id)
        char_ids = self._character_id_map(production_id)
        with self._write_lock, self.session() as s:
            for i, scene in enumerate(scenes):
                row = ScreenplayScene(
                    segment_id=segment_id, order_index=i, slug=scene.id, synthetic=True,
                    chapter=scene.chapter, title=scene.title, summary=scene.summary,
                    setting=scene.setting, time_of_day=scene.time_of_day, mood=scene.mood,
                    source_excerpt=scene.source_excerpt,
                    start_time=scene.start_time, end_time=scene.end_time,
                )
                s.add(row)
                s.commit()
                s.refresh(row)
                shot = Shot(
                    segment_id=segment_id, scene_id=int(row.id), order_index=0,
                    slug=scene.id, shot_type=scene.shot_type,
                    # Empty move => the video uses the global Ken Burns (legacy
                    # behavior); only director shots carry an explicit camera move.
                    camera_move="",
                    visual_description=scene.visual_description,
                    start_time=scene.start_time, end_time=scene.end_time,
                )
                s.add(shot)
                s.commit()
                s.refresh(shot)
                self._link_characters(
                    s, scene.characters_present, char_ids,
                    scene_id=int(row.id), shot_id=int(shot.id),
                )
            s.commit()

    def persist_screenplay(self, production_id: int, segment_id: int, scenes: list) -> None:
        """Persist real screenplay scenes, each carrying one or more shots.

        ``scenes`` are crew ``SceneDraft`` objects (each with ``.shots`` of
        ``ShotDraft``). Replaces the segment's prior content. Shot ``order_index``
        is a single running counter across the segment so reads stay correctly
        ordered across scene boundaries; slugs encode scene/shot for stable frame
        filenames.
        """
        self.clear_segment(segment_id)
        char_ids = self._character_id_map(production_id)
        prefix = self._segment_label(segment_id)
        with self._write_lock, self.session() as s:
            shot_order = 0
            for si, scene in enumerate(scenes):
                row = ScreenplayScene(
                    segment_id=segment_id, order_index=si,
                    slug=f"{prefix}_sc{si:03d}", synthetic=False,
                    chapter=getattr(scene, "chapter", None),
                    heading=scene.heading, title=scene.title, summary=scene.summary,
                    setting=scene.setting, time_of_day=scene.time_of_day, mood=scene.mood,
                    action=scene.action, source_excerpt=scene.source_excerpt,
                    start_time=scene.start_time, end_time=scene.end_time,
                    revision=getattr(scene, "revision", 0),
                )
                s.add(row)
                s.commit()
                s.refresh(row)
                scene_cids = self._link_ids(scene.characters_present, char_ids)
                for cid in scene_cids:
                    s.add(SceneCharacterLink(scene_id=int(row.id), character_id=cid))
                for sj, shot in enumerate(scene.shots):
                    shot_row = Shot(
                        segment_id=segment_id, scene_id=int(row.id), order_index=shot_order,
                        slug=f"{prefix}_sc{si:03d}_sh{sj:02d}",
                        shot_type=shot.shot_type, camera_move=shot.camera_move,
                        composition=shot.composition, subject=shot.subject,
                        visual_description=shot.visual_description,
                        duration_weight=shot.duration_weight,
                        start_time=shot.start_time, end_time=shot.end_time,
                    )
                    s.add(shot_row)
                    s.commit()
                    s.refresh(shot_row)
                    for cid in self._link_ids(shot.characters_present, char_ids):
                        s.add(ShotCharacterLink(shot_id=int(shot_row.id), character_id=cid))
                    shot_order += 1
            s.commit()

    def _segment_label(self, segment_id: int) -> str:
        with self.session() as s:
            seg = s.get(Segment, segment_id)
            return seg.chunk_label if seg else "full"

    @staticmethod
    def _link_ids(mentions: list[str], char_ids: dict[str, int]) -> list[int]:
        out: list[int] = []
        for mention in mentions:
            cid = char_ids.get(_norm(mention))
            if cid is not None and cid not in out:
                out.append(cid)
        return out

    def _character_id_map(self, production_id: int) -> dict[str, int]:
        """name/alias (lowercased) -> character id."""
        out: dict[str, int] = {}
        with self.session() as s:
            for r in s.exec(
                select(Character).where(Character.production_id == production_id)
            ).all():
                out[_norm(r.name)] = int(r.id)
                for a in r.aliases:
                    out.setdefault(_norm(a), int(r.id))
        return out

    @staticmethod
    def _link_characters(
        s: Session, mentions: list[str], char_ids: dict[str, int],
        scene_id: int, shot_id: int,
    ) -> None:
        linked: set[int] = set()
        for mention in mentions:
            cid = char_ids.get(_norm(mention))
            if cid is None or cid in linked:
                continue
            linked.add(cid)
            s.add(SceneCharacterLink(scene_id=scene_id, character_id=cid))
            s.add(ShotCharacterLink(shot_id=shot_id, character_id=cid))

    _EDITABLE_SHOT_FIELDS = (
        "visual_description", "camera_move", "shot_type", "composition", "subject",
    )

    def update_shot(self, segment_id: int, slug: str, fields: dict) -> bool:
        """Edit a shot's creative fields (from the web UI). Returns False if unknown."""
        with self._write_lock, self.session() as s:
            shot = s.exec(
                select(Shot).where(Shot.segment_id == segment_id, Shot.slug == slug)
            ).first()
            if shot is None:
                return False
            for key in self._EDITABLE_SHOT_FIELDS:
                if key in fields and fields[key] is not None:
                    setattr(shot, key, str(fields[key]))
            s.add(shot)
            s.commit()
        return True

    def split_shot(self, segment_id: int, slug: str, at: float = 0.5) -> Optional[tuple[str, str]]:
        """Split one shot's time window in two. The original keeps ``[start, mid]``;
        a new shot (same creative fields + character links) takes ``[mid, end]``.
        Returns ``(original_slug, new_slug)`` or ``None`` if the shot is unknown."""
        at = min(0.9, max(0.1, at))
        with self._write_lock, self.session() as s:
            shot = s.exec(
                select(Shot).where(Shot.segment_id == segment_id, Shot.slug == slug)
            ).first()
            if shot is None:
                return None
            start = shot.start_time if shot.start_time is not None else 0.0
            end = shot.end_time if shot.end_time is not None else start
            mid = start + (end - start) * at
            existing = set(s.exec(
                select(Shot.slug).where(Shot.segment_id == segment_id)).all())
            new_slug = _unique_slug(slug, existing)
            new = Shot(
                segment_id=segment_id, scene_id=shot.scene_id, order_index=shot.order_index,
                slug=new_slug, shot_type=shot.shot_type, camera_move=shot.camera_move,
                composition=shot.composition, subject=shot.subject,
                visual_description=shot.visual_description,
                duration_weight=shot.duration_weight, start_time=mid, end_time=end,
            )
            shot.end_time = mid
            s.add(shot)
            s.add(new)
            s.commit()
            s.refresh(new)
            for cid in s.exec(select(ShotCharacterLink.character_id).where(
                    ShotCharacterLink.shot_id == shot.id)).all():
                s.add(ShotCharacterLink(shot_id=int(new.id), character_id=cid))
            s.commit()
            # Renumber the segment's shots so order_index follows the timeline again.
            shots = s.exec(select(Shot).where(Shot.segment_id == segment_id)).all()
            for i, sh in enumerate(sorted(
                    shots, key=lambda r: (r.start_time if r.start_time is not None else 0.0, r.slug))):
                sh.order_index = i
                s.add(sh)
            s.commit()
        return (slug, new_slug)

    def has_shots(self, segment_id: int) -> bool:
        with self.session() as s:
            return s.exec(select(Shot.id).where(Shot.segment_id == segment_id)).first() is not None

    # -- frames -------------------------------------------------------------

    def upsert_frame(self, segment_id: int, frame: FrameDTO, cache_key: Optional[str] = None) -> None:
        """Persist one frame, keyed by its shot's slug (``frame.scene_id``)."""
        with self._write_lock, self.session() as s:
            shot = s.exec(
                select(Shot).where(Shot.segment_id == segment_id, Shot.slug == frame.scene_id)
            ).first()
            if shot is None:
                return
            row = s.exec(select(Frame).where(Frame.shot_id == shot.id)).first()
            if row is None:
                row = Frame(shot_id=int(shot.id))
            row.prompt = frame.prompt
            row.image_path = frame.image_path
            row.provider = frame.provider
            row.model = frame.model
            row.references = list(frame.references)
            row.cached = frame.cached
            row.error = frame.error
            row.cache_key = cache_key
            s.add(row)
            s.commit()

    def frame_cache(self, segment_id: int, slug: str) -> Optional[tuple[Optional[str], Optional[str]]]:
        """``(image_path, cache_key)`` for a shot's frame, or ``None`` if unrendered."""
        with self.session() as s:
            shot = s.exec(
                select(Shot).where(Shot.segment_id == segment_id, Shot.slug == slug)
            ).first()
            if shot is None:
                return None
            row = s.exec(select(Frame).where(Frame.shot_id == shot.id)).first()
            return (row.image_path, row.cache_key) if row else None

    def get_frame(self, segment_id: int, slug: str) -> Optional[FrameDTO]:
        with self.session() as s:
            shot = s.exec(
                select(Shot).where(Shot.segment_id == segment_id, Shot.slug == slug)
            ).first()
            if shot is None:
                return None
            row = s.exec(select(Frame).where(Frame.shot_id == shot.id)).first()
            if row is None:
                return None
            return _frame_dto(row, slug)

    # -- aggregate reads (web / gallery / video / cli) ----------------------

    def production_view(self, production_id: Optional[int] = None) -> Optional[ProductionView]:
        with self.session() as s:
            prod = (
                s.get(Production, production_id) if production_id
                else s.exec(select(Production)).first()
            )
            if prod is None:
                return None
            pid = int(prod.id)
            characters = self._characters(s, pid)
            char_name = {int(r.id): r.name for r in s.exec(
                select(Character).where(Character.production_id == pid)).all()}
            segments = s.exec(select(Segment).where(Segment.production_id == pid)).all()
            segments = sorted(segments, key=lambda r: (r.start, r.chunk_label))
            seg_views = [self._segment_view(s, seg, char_name) for seg in segments]
            return ProductionView(prod.title, prod.author, characters, seg_views)

    def segment_view(self, segment_id: int) -> Optional[SegmentView]:
        with self.session() as s:
            seg = s.get(Segment, segment_id)
            if seg is None:
                return None
            char_name = {int(r.id): r.name for r in s.exec(
                select(Character).where(Character.production_id == seg.production_id)).all()}
            return self._segment_view(s, seg, char_name)

    def segment_scenes(self, segment_id: int) -> list[Scene]:
        """The render-unit DTOs (shots -> Scene) for one segment."""
        view = self.segment_view(segment_id)
        return [sv.to_scene() for sv in view.shots] if view else []

    def load_segment_analysis(self, production_id: int, segment_id: int) -> BookAnalysis:
        """A :class:`BookAnalysis` whose scenes are this segment's shots + the full bible."""
        with self.session() as s:
            prod = s.get(Production, production_id)
            title, author = (prod.title, prod.author) if prod else ("", "")
        return BookAnalysis(
            title=title, author=author,
            characters=self.list_characters(production_id),
            scenes=self.segment_scenes(segment_id),
        )

    @staticmethod
    def _characters(s: Session, pid: int) -> list[CharacterDTO]:
        return [
            CharacterDTO(name=r.name, aliases=list(r.aliases),
                         description=r.description, role=r.role)
            for r in s.exec(select(Character).where(Character.production_id == pid)).all()
        ]

    @staticmethod
    def _segment_view(s: Session, seg: Segment, char_name: dict[int, str]) -> SegmentView:
        scenes = {
            int(sc.id): sc for sc in s.exec(
                select(ScreenplayScene).where(ScreenplayScene.segment_id == seg.id)
            ).all()
        }
        shots = s.exec(select(Shot).where(Shot.segment_id == seg.id)).all()
        shots = sorted(shots, key=lambda r: (r.order_index, r.slug))
        shot_views: list[ShotView] = []
        for shot in shots:
            frame_row = s.exec(select(Frame).where(Frame.shot_id == shot.id)).first()
            names = [
                char_name[cid] for cid in s.exec(
                    select(ShotCharacterLink.character_id).where(
                        ShotCharacterLink.shot_id == shot.id)
                ).all() if cid in char_name
            ]
            shot_views.append(_shot_view(shot, scenes.get(int(shot.scene_id)), names, frame_row))
        return SegmentView(seg.chunk_label, seg.start, seg.end, shot_views)

    # -- continuity ---------------------------------------------------------

    def add_continuity_notes(self, segment_id: int, notes: list[dict]) -> None:
        with self._write_lock, self.session() as s:
            for n in notes:
                s.add(ContinuityNote(
                    segment_id=segment_id,
                    severity=str(n.get("severity", "info")),
                    category=str(n.get("category", "")),
                    message=str(n.get("message", "")),
                    proposed_fix=n.get("proposed_fix"),
                    status=str(n.get("status", "open")),
                ))
            s.commit()

    def list_continuity_notes(
        self, segment_id: int, status: Optional[str] = None
    ) -> list[ContinuityNote]:
        with self.session() as s:
            q = select(ContinuityNote).where(ContinuityNote.segment_id == segment_id)
            if status:
                q = q.where(ContinuityNote.status == status)
            return list(s.exec(q).all())

    # -- render jobs (async re-render queue) --------------------------------

    def create_render_job(
        self, segment_id: int, shot_slug: str, now: float,
        prompt_override: Optional[str] = None, style_override: Optional[str] = None,
    ) -> int:
        with self._write_lock, self.session() as s:
            job = RenderJob(
                segment_id=segment_id, shot_slug=shot_slug,
                prompt_override=prompt_override, style_override=style_override,
                status="queued", created_at=now, updated_at=now,
            )
            s.add(job)
            s.commit()
            return int(job.id)

    def update_render_job(
        self, job_id: int, status: str, now: float,
        error: Optional[str] = None, image_path: Optional[str] = None,
    ) -> None:
        with self._write_lock, self.session() as s:
            job = s.get(RenderJob, job_id)
            if job is None:
                return
            job.status = status
            job.updated_at = now
            if error is not None:
                job.error = error
            if image_path is not None:
                job.image_path = image_path
            s.add(job)
            s.commit()

    def get_render_job(self, job_id: int) -> Optional[JobView]:
        with self.session() as s:
            job = s.get(RenderJob, job_id)
            return _job_view(job) if job else None

    def get_render_job_request(self, job_id: int) -> Optional[tuple[int, str, Optional[str], Optional[str]]]:
        """``(segment_id, shot_slug, prompt_override, style_override)`` for the worker."""
        with self.session() as s:
            job = s.get(RenderJob, job_id)
            if job is None:
                return None
            return (job.segment_id, job.shot_slug, job.prompt_override, job.style_override)

    def list_render_jobs(self, segment_id: int, active_only: bool = False) -> list[JobView]:
        with self.session() as s:
            q = select(RenderJob).where(RenderJob.segment_id == segment_id)
            if active_only:
                q = q.where(RenderJob.status.in_(("queued", "running")))
            rows = s.exec(q).all()
        rows.sort(key=lambda r: r.created_at)
        return [_job_view(r) for r in rows]


# -- helpers -----------------------------------------------------------------


def _unique_slug(base: str, existing: set[str]) -> str:
    for n in range(2, 1000):
        candidate = f"{base}-{n}"
        if candidate not in existing:
            return candidate
    return f"{base}-x"


def _enable_sqlite_pragmas(engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


def _job_view(job: RenderJob) -> JobView:
    return JobView(
        id=int(job.id), shot_slug=job.shot_slug, status=job.status, error=job.error,
        image_path=job.image_path, created_at=job.created_at, updated_at=job.updated_at,
    )


def _frame_dto(row: Frame, slug: str) -> FrameDTO:
    return FrameDTO(
        scene_id=slug, prompt=row.prompt, image_path=row.image_path,
        provider=row.provider, model=row.model, references=list(row.references),
        cached=row.cached, error=row.error,
    )


def _shot_view(
    shot: Shot, scene: Optional[ScreenplayScene], names: list[str], frame_row: Optional[Frame]
) -> ShotView:
    frame = _frame_dto(frame_row, shot.slug) if frame_row else None
    sc = scene
    return ShotView(
        slug=shot.slug,
        order_index=shot.order_index,
        scene_slug=sc.slug if sc else "",
        chapter=sc.chapter if sc else None,
        heading=sc.heading if sc else "",
        title=sc.title if sc else "",
        summary=sc.summary if sc else "",
        setting=sc.setting if sc else "",
        time_of_day=sc.time_of_day if sc else "",
        mood=sc.mood if sc else "",
        shot_type=shot.shot_type,
        camera_move=shot.camera_move,
        composition=shot.composition,
        subject=shot.subject,
        visual_description=shot.visual_description,
        source_excerpt=sc.source_excerpt if sc else "",
        characters_present=names,
        start_time=shot.start_time,
        end_time=shot.end_time,
        frame=frame,
    )
