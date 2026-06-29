"""End-to-end pipeline orchestration.

    ingest -> analyze -> (align) -> build prompts -> generate -> gallery

Each stage is optional/guarded so the pipeline degrades gracefully: no audio?
skip alignment. Analysis-only? stop before generation. Generation failure on
one frame doesn't abort the rest.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .analysis import Analyzer
from .analysis.align import align_scenes_to_structure
from .config import Config
from .gallery import render_gallery
from .generation import build_portrait_prompt, build_prompt, cache, get_provider
from .ingest import (
    audio_file_boundaries,
    build_audiobook_structure,
    transcribe_audio,
)
from .models import (
    AudiobookStructure,
    BookAnalysis,
    Character,
    Frame,
    Scene,
    Transcript,
)
from .store import ProductionStore

ProgressFn = Callable[[str], None]

_WINDOW_RE = re.compile(r"(\d+)-(\d+)min")
_WINDOW_END_RE = re.compile(r"(\d+)-endmin")


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name.strip().lower()).strip("_") or "x"


def _chunk_label(structure_path: str) -> str:
    """A stable per-chunk label from a structure filename (for ids + subfolders)."""
    stem = Path(structure_path).stem
    label = re.sub(r"^audiobook_structure_?", "", stem)
    return label or "full"


def _window_from_span(chunk_label: str, span_start: float, span_end: float) -> tuple[float, float]:
    """The segment's audio window. Encoded in the chunk label (e.g. ``0000-60min``)
    when present; otherwise the given content span."""
    if m := _WINDOW_RE.search(chunk_label):
        return float(m.group(1)) * 60.0, float(m.group(2)) * 60.0
    if m := _WINDOW_END_RE.search(chunk_label):
        return float(m.group(1)) * 60.0, span_end
    return span_start, span_end


def _segment_window(chunk_label: str, scenes: list[Scene]) -> tuple[float, float]:
    starts = [s.start_time for s in scenes if s.start_time is not None]
    ends = [s.end_time for s in scenes if s.end_time is not None]
    return _window_from_span(
        chunk_label, min(starts) if starts else 0.0, max(ends) if ends else 0.0
    )


def _structure_span(structure: AudiobookStructure) -> tuple[float, float]:
    paras = [p for ch in structure.chapters for p in ch.paragraphs]
    starts = [p.start for p in paras if p.start is not None]
    ends = [p.end for p in paras if p.end is not None]
    return (min(starts) if starts else 0.0), (max(ends) if ends else 0.0)


def _scene_references(
    scene: Scene, analysis: BookAnalysis, portraits: dict[str, Path]
) -> list[Path]:
    """Portrait paths for characters present in a scene (deduped, canonical)."""
    seen: set[str] = set()
    refs: list[Path] = []
    for mention in scene.characters_present:
        char = analysis.character(mention)
        keys = [mention.lower()]
        if char:
            keys = [char.name.lower(), *(a.lower() for a in char.aliases)]
        for key in keys:
            path = portraits.get(key)
            if path and str(path) not in seen:
                seen.add(str(path))
                refs.append(path)
                break
    return refs


@dataclass
class IngestResult:
    chapters: list[tuple[str, str]] = field(default_factory=list)
    title: str = ""
    author: str = ""
    transcript: Optional[Transcript] = None
    structure: Optional[AudiobookStructure] = None


class Pipeline:
    def __init__(self, config: Config, on_progress: Optional[ProgressFn] = None):
        self.config = config
        self._progress = on_progress or (lambda _m: None)

    # -- stages -------------------------------------------------------------

    def ingest(
        self,
        audio_path: Optional[str],
        structure_path: Optional[str] = None,
    ) -> IngestResult:
        result = IngestResult()

        if structure_path:
            # Reuse a previously segmented audiobook — skip (re)transcription.
            self._progress(f"Loading audiobook structure: {structure_path}")
            result.structure = AudiobookStructure.model_validate_json(
                Path(structure_path).read_text()
            )
            if not result.chapters:
                result.chapters = result.structure.as_chapters
                result.title = result.title or result.structure.title
            n_para = sum(len(c.paragraphs) for c in result.structure.chapters)
            self._progress(
                f"  {len(result.structure.chapters)} chapter(s), {n_para} paragraph(s) "
                f"(loaded, via {result.structure.source})"
            )
        elif audio_path and self.config.audio.enabled:
            self._progress(
                f"Transcribing audio ({self.config.audio.backend}): {audio_path}"
            )
            result.transcript = transcribe_audio(
                audio_path,
                backend=self.config.audio.backend,
                model=self.config.audio.model,
                on_progress=self._transcription_progress(),
                chunk_seconds=self.config.audio.chunk_seconds,
            )
            self._progress(f"  {len(result.transcript.segments)} transcript segment(s)")

            # Audiobook-only mode: the transcript is the text source. Recover
            # chapter/paragraph structure from the audio instead of using one
            # undifferentiated blob.
            if not result.chapters:
                result.title = result.title or Path(audio_path).stem
                if self.config.audio.segment:
                    self._progress("Segmenting audiobook into chapters and paragraphs")
                    # A folder of parts -> one chapter per file.
                    boundaries = audio_file_boundaries(audio_path)
                    result.structure = build_audiobook_structure(
                        result.transcript, audio_path, self.config.audio,
                        title=result.title, file_boundaries=boundaries,
                    )
                    result.chapters = result.structure.as_chapters
                    n_para = sum(len(c.paragraphs) for c in result.structure.chapters)
                    self._progress(
                        f"  {len(result.chapters)} chapter(s), {n_para} paragraph(s) "
                        f"(via {result.structure.source})"
                    )
                else:
                    result.chapters = [("Audiobook transcript", result.transcript.full_text)]

        if not result.chapters:
            raise ValueError("Nothing to analyze: provide an audiobook or a structure file.")
        return result

    def _transcription_progress(self) -> Callable[[float, float], None]:
        """A throttled (per-decile) text progress reporter for transcription."""
        state = {"last_decile": -1}

        def report(done: float, total: float) -> None:
            if not total:
                return
            decile = int(done / total * 10)
            if decile > state["last_decile"]:
                state["last_decile"] = decile
                self._progress(
                    f"  transcribing… {done / total * 100:3.0f}% "
                    f"({done:.0f}s / {total:.0f}s)"
                )

        return report

    def analyze(
        self,
        chapters: list[tuple[str, str]],
        title: str,
        author: str,
        structure: Optional[AudiobookStructure] = None,
        id_prefix: str = "scene",
        known_characters: Optional[list[Character]] = None,
    ) -> BookAnalysis:
        analyzer = Analyzer(self.config.analysis, on_progress=self._progress)

        # Per-paragraph mode: one frame per paragraph, timestamps taken straight
        # from the structure — no alignment step needed. Needs a structure.
        if self.config.analysis.mode == "paragraphs":
            if structure is not None:
                return analyzer.analyze_paragraphs(
                    structure, title=title, author=author,
                    id_prefix=id_prefix, known_characters=known_characters,
                )
            self._progress(
                "  (paragraph mode needs an audiobook structure; using scene mode)"
            )

        analysis = analyzer.analyze(
            chapters, title=title, author=author,
            id_prefix=id_prefix, known_characters=known_characters,
        )

        if structure is not None:
            # Exact timestamps straight from the source paragraphs.
            self._progress("Aligning scenes to paragraph timestamps")
            align_scenes_to_structure(analysis.scenes, structure)
        return analysis

    def _run_crew(
        self, structure: AudiobookStructure, store: ProductionStore, production_id: int,
        chunk_label: str, structure_path: Optional[str], known: list[Character],
    ) -> int:
        """Director mode: a crew (screenwriter -> director) turns the structure into
        a screenplay broken into shots, persisted as real scenes/shots. Returns the
        segment id."""
        from .crew import AgentRunner, Crew

        # Build (and accumulate) the character bible first — the crew needs it.
        analyzer = Analyzer(self.config.analysis, on_progress=self._progress)
        chunks = analyzer._chunk(structure.as_chapters)
        self._progress(f"Building character bible from {len(chunks)} chunk(s)")
        characters = analyzer._build_character_bible(chunks, known)
        store.upsert_characters(production_id, characters)
        characters = store.list_characters(production_id)

        span_start, span_end = _structure_span(structure)
        start, end = _window_from_span(chunk_label, span_start, span_end)
        segment_id = store.get_or_create_segment(
            production_id, chunk_label, start, end, structure_path
        )

        runner = AgentRunner(self.config.analysis, agent_mode=self.config.crew.agent_mode)
        crew = Crew(self.config, runner, on_progress=self._progress)
        self._progress("Crew: writing the screenplay and breaking it into shots")
        scenes = crew.build(structure, characters)
        store.persist_screenplay(production_id, segment_id, scenes)
        total_shots = sum(len(s.shots) for s in scenes)
        self._progress(
            f"Persisted {len(scenes)} screenplay scene(s), {total_shots} shot(s) to production.db"
        )

        # Continuity supervisor: flag inconsistencies across the built shot list.
        notes = crew.review_continuity(scenes, characters)
        if notes:
            store.add_continuity_notes(segment_id, notes)
            self._progress(f"Logged {len(notes)} continuity note(s) to production.db")
        return segment_id

    def generate(
        self,
        analysis: BookAnalysis,
        out_dir: Path,
        store: Optional[ProductionStore] = None,
        production_id: Optional[int] = None,
        segment_id: Optional[int] = None,
        book_dir: Optional[Path] = None,
        dry_run: bool = False,
        force: bool = False,
    ) -> list[Frame]:
        # Portraits (and the character bible) are shared at the book level so
        # they're cached/reused across separately-rendered chunks; frames are
        # per-chunk under out_dir and persisted to the production DB.
        book_dir = Path(book_dir) if book_dir is not None else Path(out_dir)
        out_dir = Path(out_dir)
        if store is None:
            # Standalone use: provision the production/segment from the analysis so
            # callers can render straight from a BookAnalysis. run() passes its own.
            store, production_id, segment_id = self._provision(analysis, out_dir, book_dir)
        gen_cfg = self.config.generation
        provider = get_provider(gen_cfg, dry_run=dry_run)
        frames_dir = out_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)

        scenes = analysis.scenes
        if gen_cfg.max_frames > 0:
            scenes = scenes[: gen_cfg.max_frames]

        # Character portraits (consistency) — only when the provider can use them,
        # and only for characters that appear in the frames we're about to render.
        portraits: dict[str, Path] = {}
        if gen_cfg.character_portraits and provider.supports_references:
            needed = {m.lower() for s in scenes for m in s.characters_present}
            portraits = self._generate_portraits(
                analysis, provider, book_dir, force, needed, store, production_id
            )

        self._progress(
            f"Generating {len(scenes)} frame(s) via "
            f"{'stub (dry-run)' if dry_run else provider.name}"
        )

        use_refs = provider.supports_references and bool(portraits)
        jobs = []
        for scene in scenes:
            prompt = build_prompt(
                scene,
                analysis,
                self.config.project.style,
                include_shot=gen_cfg.shot_variety,
                with_references=use_refs,
            )
            refs = _scene_references(scene, analysis, portraits) if use_refs else []
            jobs.append((scene, prompt, refs))

        frames: list[Frame] = []
        workers = max(1, gen_cfg.concurrency)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {
                pool.submit(
                    self._render_one, provider, scene, prompt, refs, frames_dir,
                    force, store, segment_id,
                ): scene
                for scene, prompt, refs in jobs
            }
            done = 0
            for future in as_completed(future_map):
                frame = future.result()
                frames.append(frame)
                done += 1
                if frame.cached:
                    status = "cached"
                elif frame.ok:
                    status = "ok"
                else:
                    status = f"FAILED ({frame.error})"
                self._progress(f"  [{done}/{len(jobs)}] {frame.scene_id}: {status}")

        # Restore scene order (futures complete out of order).
        order = {scene.id: i for i, scene in enumerate(scenes)}
        frames.sort(key=lambda f: order.get(f.scene_id, 1_000_000))
        return frames

    def _provision(
        self, analysis: BookAnalysis, out_dir: Path, book_dir: Path
    ) -> tuple[ProductionStore, int, int]:
        """Open the book's store and ensure a segment with this analysis' shots.

        Used when :meth:`generate` is called without an explicit store (standalone
        rendering). Scenes are only (re)persisted when the segment has none yet, so
        re-rendering the same analysis reuses cached frames instead of wiping them.
        """
        store = ProductionStore.open(book_dir)
        production_id = store.get_or_create_production(
            title=analysis.title, author=analysis.author, style=self.config.project.style
        )
        chunk_label = out_dir.name if out_dir != book_dir else "full"
        segment_id = store.find_segment(production_id, chunk_label)
        if segment_id is None or not store.has_shots(segment_id):
            store.upsert_characters(production_id, analysis.characters)
            start, end = _segment_window(chunk_label, analysis.scenes)
            segment_id = store.get_or_create_segment(production_id, chunk_label, start, end)
            store.persist_scenes_as_shots(production_id, segment_id, analysis.scenes)
        return store, production_id, segment_id

    def _generate_portraits(
        self, analysis: BookAnalysis, provider, book_dir: Path, force: bool, needed: set[str],
        store: Optional[ProductionStore] = None, production_id: Optional[int] = None,
    ) -> dict[str, Path]:
        """Render one reference portrait per described character that appears.

        Portraits are keyed by an appearance hash, so an unchanged look is reused
        across chunks (cached) while a changed look produces a new portrait —
        supporting characters whose appearance evolves over the book.
        """
        portraits_dir = book_dir / "portraits"
        portraits_dir.mkdir(parents=True, exist_ok=True)

        def appears(char) -> bool:
            names = {char.name.lower(), *(a.lower() for a in char.aliases)}
            return bool(names & needed)

        characters = [c for c in analysis.characters if c.description and appears(c)]
        if not characters:
            return {}
        self._progress(f"Rendering {len(characters)} character portrait(s)")

        portrait_size = self.config.generation.portrait_size
        out: dict[str, Path] = {}
        for char in characters:
            prompt = build_portrait_prompt(char, self.config.project.style)
            key = cache.compute_key(
                {
                    "prompt": prompt,
                    "provider": provider.name,
                    "model": provider.model,
                    "size": portrait_size,
                }
            )
            # Appearance hash in the filename: same look -> same file (shared and
            # cached across chunks); changed look -> a new portrait. Existence-based:
            # delete a portrait to regenerate it.
            path = portraits_dir / f"{_slug(char.name)}_{key[:8]}.png"
            try:
                if self.config.generation.cache and not force and path.exists():
                    pass  # reuse the existing portrait
                else:
                    provider.generate(prompt, path, size=portrait_size)
                # Map canonical name and aliases to the portrait.
                out[char.name.lower()] = path
                for alias in char.aliases:
                    out[alias.lower()] = path
                if store is not None and production_id is not None:
                    store.set_portrait(production_id, char.name, str(path), key)
            except Exception as exc:  # noqa: BLE001 - a missing portrait isn't fatal
                self._progress(f"  (portrait failed for {char.name}: {exc})")
        return out

    def regenerate_frame(
        self,
        analysis: BookAnalysis,
        out_dir: Path,
        store: ProductionStore,
        production_id: int,
        segment_id: int,
        scene_id: str,
        prompt_override: Optional[str] = None,
        style_override: Optional[str] = None,
        dry_run: bool = False,
    ) -> Frame:
        """Render a single shot's frame (used by the web UI). Always force.

        ``prompt_override`` lets the UI hand-edit the prompt; ``style_override``
        swaps the visual style for this one frame.
        """
        out_dir = Path(out_dir)
        scene = next((s for s in analysis.scenes if s.id == scene_id), None)
        if scene is None:
            raise KeyError(scene_id)

        gen_cfg = self.config.generation
        provider = get_provider(gen_cfg, dry_run=dry_run)
        frames_dir = out_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)

        portraits: dict[str, Path] = {}
        if gen_cfg.character_portraits and provider.supports_references:
            needed = {m.lower() for m in scene.characters_present}
            portraits = self._generate_portraits(
                analysis, provider, out_dir, False, needed, store, production_id
            )

        use_refs = provider.supports_references and bool(portraits)
        style = style_override if style_override is not None else self.config.project.style
        if prompt_override:
            prompt = prompt_override
        else:
            prompt = build_prompt(
                scene, analysis, style,
                include_shot=gen_cfg.shot_variety, with_references=use_refs,
            )
        refs = _scene_references(scene, analysis, portraits) if use_refs else []
        # force=True: a manual regenerate should always produce a fresh image.
        return self._render_one(
            provider, scene, prompt, refs, frames_dir, True, store, segment_id
        )

    def _render_one(
        self, provider, scene, prompt: str, refs: list[Path], frames_dir: Path, force: bool,
        store: ProductionStore, segment_id: int,
    ) -> Frame:
        frame = Frame(
            scene_id=scene.id,
            prompt=prompt,
            provider=provider.name,
            model=provider.model,
            references=[str(r) for r in refs],
        )
        key = cache.compute_key(
            {"prompt": prompt, "provider": provider.name, "model": provider.model}
        )
        try:
            # DB-driven cache: reuse only when a Frame row exists, its image is on
            # disk, AND the recomputed key matches — stronger than a bare file check.
            # Delete the image (or pass --force) to regenerate.
            if self.config.generation.cache and not force:
                cached = store.frame_cache(segment_id, scene.id)
                if cached and cached[0] and Path(cached[0]).exists() and cached[1] == key:
                    frame.image_path = cached[0]
                    frame.cached = True
                    store.upsert_frame(segment_id, frame, key)
                    return frame
            path = provider.generate(prompt, frames_dir / scene.id, references=refs)
            frame.image_path = str(path)
        except Exception as exc:  # noqa: BLE001 - one frame failing shouldn't abort
            frame.error = str(exc)
        store.upsert_frame(segment_id, frame, key)
        return frame

    # -- full run -----------------------------------------------------------

    def run(
        self,
        audio_path: Optional[str] = None,
        structure_path: Optional[str] = None,
        segment_label: Optional[str] = None,
        analyze_only: bool = False,
        dry_run: bool = False,
        force: bool = False,
        reanalyze: bool = False,
    ) -> BookAnalysis:
        # book_dir holds the production database (the source of truth: characters,
        # scenes, shots, frames) + shared portraits; out_dir is the per-chunk folder
        # where frame images and the compiled video live, so separately rendered
        # chunks don't overwrite each other.
        book_dir = Path(self.config.output.dir)
        chunk_label = (
            _chunk_label(structure_path) if structure_path else (segment_label or "full")
        )
        out_dir = (book_dir / chunk_label) if chunk_label != "full" else book_dir
        book_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)

        store = ProductionStore.open(book_dir)
        production_id = store.get_or_create_production(style=self.config.project.style)
        segment_id = store.find_segment(production_id, chunk_label)

        # Skip (re)analysis if this segment already has shots — reuse what's in the
        # production DB and go straight to generation.
        if segment_id is not None and store.has_shots(segment_id) and not reanalyze:
            self._progress(f"Reusing existing analysis for segment '{chunk_label}' (production.db)")
            self._progress("  (pass --reanalyze to regenerate it from the source)")
            analysis = store.load_segment_analysis(production_id, segment_id)
        else:
            # Re-analyse straight from the structure stored in the DB when no source
            # is supplied (so a structure file is only needed the first time).
            stored = store.get_segment_structure(segment_id) if segment_id is not None else None
            if reanalyze and not structure_path and not audio_path and stored:
                self._progress("Re-analyzing from the structure stored in production.db")
                structure = AudiobookStructure.model_validate_json(stored)
                ingested = IngestResult(
                    chapters=structure.as_chapters, title=structure.title, structure=structure
                )
            else:
                ingested = self.ingest(audio_path, structure_path)

            if ingested.structure is not None and not structure_path:
                # Freshly segmented (not reusing a structure): persist at book level.
                structure_out = book_dir / "audiobook_structure.json"
                structure_out.write_text(
                    ingested.structure.model_dump_json(indent=2), encoding="utf-8"
                )
                self._progress(f"Wrote audiobook structure -> {structure_out}")

            # Continuity: seed analysis with the accumulated character bible.
            known = store.list_characters(production_id)
            store.get_or_create_production(
                title=ingested.title, author=ingested.author,
                source="audiobook", style=self.config.project.style,
            )

            if self.config.analysis.mode == "director" and ingested.structure is not None:
                segment_id = self._run_crew(
                    ingested.structure, store, production_id, chunk_label, structure_path, known
                )
            else:
                if self.config.analysis.mode == "director":
                    self._progress("  (director mode needs an audiobook structure; using scene mode)")
                analysis = self.analyze(
                    ingested.chapters, ingested.title, ingested.author,
                    ingested.structure,
                    id_prefix=chunk_label if chunk_label != "full" else "scene",
                    known_characters=known,
                )
                store.upsert_characters(production_id, analysis.characters)
                start, end = _segment_window(chunk_label, analysis.scenes)
                segment_id = store.get_or_create_segment(
                    production_id, chunk_label, start, end, structure_path
                )
                store.persist_scenes_as_shots(production_id, segment_id, analysis.scenes)
                self._progress(f"Persisted {len(analysis.scenes)} shot(s) to production.db")

            # Keep the source structure in the DB so re-analysis needs no file.
            if ingested.structure is not None:
                store.set_segment_structure(segment_id, ingested.structure.model_dump_json())

            # Reload so characters_present are canonicalised against the bible.
            analysis = store.load_segment_analysis(production_id, segment_id)

        if analyze_only:
            return analysis

        self.generate(
            analysis, out_dir, store=store, production_id=production_id,
            segment_id=segment_id, book_dir=book_dir, dry_run=dry_run, force=force,
        )

        if self.config.output.gallery:
            gallery_path = render_gallery(store.production_view(production_id), out_dir)
            self._progress(f"Wrote gallery -> {gallery_path}")

        return analysis
