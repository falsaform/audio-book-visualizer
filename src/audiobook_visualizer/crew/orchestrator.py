"""The crew orchestrator: a screenwriter and a director collaborate to turn a
timestamped audiobook structure into a screenplay broken down shot by shot.

The orchestrator runs the configured roles (currently script_writer -> director),
validates their structured output, and pins shot timings deterministically so the
shots tile each scene's audio window. It returns plain drafts; persistence to the
production store happens in the pipeline (agents never touch the DB).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from ..config import Config, RoleConfig
from ..models import AudiobookStructure, Character
from . import prompts
from .agent import AgentRunner
from .timing import allocate_shot_timings, target_shot_count

ProgressFn = Callable[[str], None]
_MOVES = ", ".join(prompts.CAMERA_MOVES)


@dataclass
class ShotDraft:
    shot_type: str = ""
    camera_move: str = "static"
    composition: str = ""
    subject: str = ""
    visual_description: str = ""
    characters_present: list[str] = field(default_factory=list)
    duration_weight: float = 1.0
    start_time: Optional[float] = None
    end_time: Optional[float] = None


@dataclass
class SceneDraft:
    heading: str = ""
    title: str = ""
    summary: str = ""
    action: str = ""
    setting: str = ""
    time_of_day: str = ""
    mood: str = ""
    chapter: Optional[str] = None
    characters_present: list[str] = field(default_factory=list)
    source_excerpt: str = ""
    start_time: float = 0.0
    end_time: float = 0.0
    narration: str = ""  # transient: fed to the director, not persisted
    shots: list[ShotDraft] = field(default_factory=list)


class Crew:
    def __init__(self, config: Config, runner: AgentRunner, on_progress: Optional[ProgressFn] = None):
        self.config = config
        self.runner = runner
        self.timing = config.crew.timing
        self._roles = {r.name: r for r in config.crew.roles}
        self._progress = on_progress or (lambda _m: None)

    def build(
        self, structure: AudiobookStructure, characters: list[Character]
    ) -> list[SceneDraft]:
        writer = self._roles.get("script_writer")
        director = self._roles.get("director")
        char_summary = _character_summary(characters)

        scenes: list[SceneDraft] = []
        for ci, chapter in enumerate(structure.chapters, 1):
            paras = [p for p in chapter.paragraphs if p.text.strip()]
            if not paras:
                continue
            if writer and writer.enabled:
                drafts = self._write_scenes(writer, chapter.title, paras, char_summary)
            else:
                drafts = [_whole_chapter_scene(chapter.title, paras)]

            for sd in drafts:
                if director and director.enabled:
                    sd.shots = self._direct(director, sd, char_summary)
                if not sd.shots:
                    sd.shots = [_fallback_shot(sd)]
                self._assign_timings(sd)
            scenes.extend(drafts)
            self._progress(
                f"  chapter {ci}: {len(drafts)} scene(s), "
                f"{sum(len(s.shots) for s in drafts)} shot(s)"
            )
        return scenes

    # -- script writer ------------------------------------------------------

    def _write_scenes(
        self, role: RoleConfig, chapter_title: str, paras, char_summary: str
    ) -> list[SceneDraft]:
        numbered = "\n".join(f"[{i}] {p.text}" for i, p in enumerate(paras))
        note = f" '{chapter_title}'" if chapter_title else ""
        try:
            data = self.runner.json(
                role, prompts.SCRIPT_WRITER_SYSTEM,
                prompts.SCRIPT_WRITER_PROMPT.format(
                    characters=char_summary or "(none identified)",
                    chapter_note=note, paragraphs=numbered,
                ),
                max_tokens=4096,
            )
        except Exception as exc:  # noqa: BLE001
            self._progress(f"  (script writer failed: {exc})")
            return [_whole_chapter_scene(chapter_title, paras)]

        items = [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
        if not items:
            return [_whole_chapter_scene(chapter_title, paras)]

        # Normalise start paragraphs into a strictly increasing, in-range sequence
        # so scenes contiguously tile the chapter (monotonic repair).
        starts = sorted(max(0, min(int(d.get("start_paragraph", 0) or 0), len(paras) - 1))
                        for d in items)
        items = sorted(items, key=lambda d: int(d.get("start_paragraph", 0) or 0))
        fixed: list[int] = []
        prev = -1
        for sp in starts:
            sp = max(sp, prev + 1)
            fixed.append(min(sp, len(paras) - 1))
            prev = fixed[-1]
        fixed[0] = 0

        drafts: list[SceneDraft] = []
        for k, d in enumerate(items):
            first = fixed[k]
            last = (fixed[k + 1] - 1) if k + 1 < len(items) else len(paras) - 1
            last = max(first, last)
            grp = paras[first : last + 1]
            start = grp[0].start if grp[0].start is not None else 0.0
            end = grp[-1].end if grp[-1].end is not None else start
            text = " ".join(p.text for p in grp)
            drafts.append(SceneDraft(
                heading=str(d.get("heading", "")).strip(),
                title=str(d.get("title", "")).strip(),
                summary=str(d.get("synopsis", "")).strip(),
                action=str(d.get("action", "")).strip(),
                setting=str(d.get("setting", "")).strip(),
                time_of_day=str(d.get("time_of_day", "")).strip(),
                mood=str(d.get("mood", "")).strip(),
                chapter=chapter_title or None,
                characters_present=_names(d.get("characters_present")),
                source_excerpt=text[:160],
                start_time=float(start), end_time=float(end),
                narration=text,
            ))
        return drafts

    # -- director -----------------------------------------------------------

    def _direct(self, role: RoleConfig, sd: SceneDraft, char_summary: str) -> list[ShotDraft]:
        n = target_shot_count(
            sd.end_time - sd.start_time,
            self.timing.target_seconds_per_shot,
            self.timing.max_shots_per_scene,
        )
        try:
            data = self.runner.json(
                role, prompts.DIRECTOR_SYSTEM.format(moves=_MOVES),
                prompts.DIRECTOR_PROMPT.format(
                    n=n, moves=_MOVES, characters=char_summary or "(none identified)",
                    heading=sd.heading or "(scene)", title=sd.title,
                    synopsis=sd.summary, action=sd.action,
                    narration=sd.narration or sd.action or sd.summary,
                ),
                max_tokens=4096,
            )
        except Exception as exc:  # noqa: BLE001
            self._progress(f"  (director failed on '{sd.title}': {exc})")
            return []

        items = [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
        shots: list[ShotDraft] = []
        for d in items[: self.timing.max_shots_per_scene]:
            shots.append(ShotDraft(
                shot_type=str(d.get("shot_type", "")).strip(),
                camera_move=_coerce_move(d.get("camera_move")),
                composition=str(d.get("composition", "")).strip(),
                subject=str(d.get("subject", "")).strip(),
                visual_description=str(d.get("visual_description", "")).strip(),
                characters_present=_names(d.get("characters_present")),
                duration_weight=_pos_float(d.get("duration_weight"), 1.0),
            ))
        return shots

    def _assign_timings(self, sd: SceneDraft) -> None:
        spans = allocate_shot_timings(
            sd.start_time, sd.end_time,
            [s.duration_weight for s in sd.shots],
            min_shot=self.timing.min_shot_seconds,
        )
        for shot, (s, e) in zip(sd.shots, spans):
            shot.start_time, shot.end_time = s, e


# -- helpers -----------------------------------------------------------------


def _whole_chapter_scene(chapter_title: str, paras) -> SceneDraft:
    start = paras[0].start if paras[0].start is not None else 0.0
    end = paras[-1].end if paras[-1].end is not None else start
    text = " ".join(p.text for p in paras)
    return SceneDraft(
        title=chapter_title or "Scene", chapter=chapter_title or None,
        action=text[:200], source_excerpt=text[:160],
        start_time=float(start), end_time=float(end), narration=text,
    )


def _fallback_shot(sd: SceneDraft) -> ShotDraft:
    return ShotDraft(
        shot_type="wide establishing shot", camera_move="static",
        visual_description=sd.summary or sd.action or sd.title,
        characters_present=list(sd.characters_present), duration_weight=1.0,
    )


def _coerce_move(value) -> str:
    move = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return move if move in prompts.CAMERA_MOVES else "static"


def _names(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(c).strip() for c in value if str(c).strip()]


def _pos_float(value, default: float) -> float:
    try:
        f = float(value)
        return f if f > 0 else default
    except (TypeError, ValueError):
        return default


def _character_summary(characters: list[Character], limit: int = 40) -> str:
    lines = []
    for char in characters[:limit]:
        alias = f" (aka {', '.join(char.aliases)})" if char.aliases else ""
        lines.append(f"- {char.name}{alias}: {char.description}")
    return "\n".join(lines)
