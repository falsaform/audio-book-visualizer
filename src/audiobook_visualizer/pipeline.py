"""End-to-end pipeline orchestration.

    ingest -> analyze -> (align) -> build prompts -> generate -> gallery

Each stage is optional/guarded so the pipeline degrades gracefully: no audio?
skip alignment. Analysis-only? stop before generation. Generation failure on
one frame doesn't abort the rest.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .analysis import Analyzer
from .analysis.align import align_scenes, align_scenes_to_structure
from .config import Config
from .gallery import render_gallery
from .generation import build_portrait_prompt, build_prompt, cache, get_provider
from .ingest import build_audiobook_structure, load_ebook, transcribe_audio
from .models import AudiobookStructure, BookAnalysis, Frame, Scene, Transcript

ProgressFn = Callable[[str], None]


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name.strip().lower()).strip("_") or "x"


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
        ebook_path: Optional[str],
        audio_path: Optional[str],
        structure_path: Optional[str] = None,
    ) -> IngestResult:
        result = IngestResult()

        if ebook_path:
            self._progress(f"Loading ebook: {ebook_path}")
            book = load_ebook(ebook_path)
            result.chapters = [(ch.title, ch.text) for ch in book.chapters]
            result.title, result.author = book.title, book.author
            self._progress(
                f"  {len(result.chapters)} chapter(s), {len(book.full_text):,} chars"
            )

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
                    result.structure = build_audiobook_structure(
                        result.transcript, audio_path, self.config.audio, title=result.title
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
            raise ValueError("Nothing to analyze: provide an ebook and/or audiobook.")
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
        transcript: Optional[Transcript],
        structure: Optional[AudiobookStructure] = None,
    ) -> BookAnalysis:
        analyzer = Analyzer(self.config.analysis, on_progress=self._progress)
        analysis = analyzer.analyze(chapters, title=title, author=author)

        if structure is not None:
            # Audiobook-only: exact timestamps straight from source paragraphs.
            self._progress("Aligning scenes to paragraph timestamps")
            align_scenes_to_structure(analysis.scenes, structure)
        elif transcript and self.config.audio.align_to_ebook:
            # Ebook + audio: fuzzy-match excerpts against the transcript.
            self._progress("Aligning scenes to audio timestamps")
            align_scenes(analysis.scenes, transcript)
        return analysis

    def generate(
        self,
        analysis: BookAnalysis,
        out_dir: Path,
        dry_run: bool = False,
        force: bool = False,
    ) -> list[Frame]:
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
            portraits = self._generate_portraits(analysis, provider, out_dir, force, needed)

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
                    self._render_one, provider, scene, prompt, refs, frames_dir, force
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

    def _generate_portraits(
        self, analysis: BookAnalysis, provider, out_dir: Path, force: bool, needed: set[str]
    ) -> dict[str, Path]:
        """Render one reference portrait per described character that appears."""
        portraits_dir = out_dir / "portraits"
        portraits_dir.mkdir(parents=True, exist_ok=True)

        def appears(char) -> bool:
            names = {char.name.lower(), *(a.lower() for a in char.aliases)}
            return bool(names & needed)

        characters = [c for c in analysis.characters if c.description and appears(c)]
        if not characters:
            return {}
        self._progress(f"Rendering {len(characters)} character portrait(s)")

        out: dict[str, Path] = {}
        for char in characters:
            prompt = build_portrait_prompt(char, self.config.project.style)
            path = portraits_dir / f"{_slug(char.name)}.png"
            key = cache.compute_key(
                {"prompt": prompt, "provider": provider.name, "model": provider.model}
            )
            try:
                if self.config.generation.cache and not force and cache.is_cached(path, key):
                    pass  # reuse the existing portrait
                else:
                    provider.generate(prompt, path)
                    cache.write_sidecar(path, key, {"character": char.name})
                # Map canonical name and aliases to the portrait.
                out[char.name.lower()] = path
                for alias in char.aliases:
                    out[alias.lower()] = path
            except Exception as exc:  # noqa: BLE001 - a missing portrait isn't fatal
                self._progress(f"  (portrait failed for {char.name}: {exc})")
        return out

    def regenerate_frame(
        self,
        analysis: BookAnalysis,
        out_dir: Path,
        scene_id: str,
        prompt_override: Optional[str] = None,
        style_override: Optional[str] = None,
        dry_run: bool = False,
    ) -> Frame:
        """Render a single scene's frame (used by the web UI). Always force.

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
            portraits = self._generate_portraits(analysis, provider, out_dir, False, needed)

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
        return self._render_one(provider, scene, prompt, refs, frames_dir, force=True)

    def _render_one(
        self, provider, scene, prompt: str, refs: list[Path], frames_dir: Path, force: bool
    ) -> Frame:
        frame = Frame(
            scene_id=scene.id,
            prompt=prompt,
            provider=provider.name,
            model=provider.model,
            references=[str(r) for r in refs],
        )
        out_path = (frames_dir / scene.id).with_suffix(".png")
        key = cache.compute_key(
            {
                "prompt": prompt,
                "provider": provider.name,
                "model": provider.model,
                "size": self.config.generation.size,
                "refs": cache.reference_digests(refs),
            }
        )
        try:
            if self.config.generation.cache and not force and cache.is_cached(out_path, key):
                frame.image_path = str(out_path)
                frame.cached = True
                return frame
            path = provider.generate(prompt, frames_dir / scene.id, references=refs)
            frame.image_path = str(path)
            cache.write_sidecar(path, key, {"prompt": prompt})
        except Exception as exc:  # noqa: BLE001 - one frame failing shouldn't abort
            frame.error = str(exc)
        return frame

    # -- full run -----------------------------------------------------------

    def run(
        self,
        ebook_path: Optional[str] = None,
        audio_path: Optional[str] = None,
        structure_path: Optional[str] = None,
        analyze_only: bool = False,
        dry_run: bool = False,
        force: bool = False,
    ) -> BookAnalysis:
        out_dir = Path(self.config.output.dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        ingested = self.ingest(ebook_path, audio_path, structure_path)

        # Persist the audiobook segmentation so each chapter/paragraph (with its
        # timestamps) can be inspected or processed individually.
        if ingested.structure is not None:
            structure_out = out_dir / "audiobook_structure.json"
            structure_out.write_text(
                ingested.structure.model_dump_json(indent=2), encoding="utf-8"
            )
            self._progress(f"Wrote audiobook structure -> {structure_out}")

        analysis = self.analyze(
            ingested.chapters,
            ingested.title,
            ingested.author,
            ingested.transcript,
            ingested.structure,
        )

        # Always persist the analysis so it can be inspected or reused.
        analysis_path = out_dir / "analysis.json"
        analysis_path.write_text(analysis.model_dump_json(indent=2), encoding="utf-8")
        self._progress(f"Wrote analysis -> {analysis_path}")

        if analyze_only:
            return analysis

        frames = self.generate(analysis, out_dir, dry_run=dry_run, force=force)

        manifest_path = out_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps([f.model_dump() for f in frames], indent=2), encoding="utf-8"
        )
        self._progress(f"Wrote manifest -> {manifest_path}")

        if self.config.output.gallery:
            gallery_path = render_gallery(analysis, frames, out_dir)
            self._progress(f"Wrote gallery -> {gallery_path}")

        return analysis
