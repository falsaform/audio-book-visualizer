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
from .analysis.align import align_scenes
from .config import Config
from .gallery import render_gallery
from .generation import build_prompt, get_provider
from .ingest import build_audiobook_structure, load_ebook, transcribe_audio
from .models import AudiobookStructure, BookAnalysis, Frame, Transcript

ProgressFn = Callable[[str], None]


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
        self, ebook_path: Optional[str], audio_path: Optional[str]
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

        if audio_path and self.config.audio.enabled:
            self._progress(
                f"Transcribing audio ({self.config.audio.backend}): {audio_path}"
            )
            result.transcript = transcribe_audio(
                audio_path,
                backend=self.config.audio.backend,
                model=self.config.audio.model,
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

    def analyze(
        self,
        chapters: list[tuple[str, str]],
        title: str,
        author: str,
        transcript: Optional[Transcript],
    ) -> BookAnalysis:
        analyzer = Analyzer(self.config.analysis, on_progress=self._progress)
        analysis = analyzer.analyze(chapters, title=title, author=author)

        if transcript and self.config.audio.align_to_ebook:
            self._progress("Aligning scenes to audio timestamps")
            align_scenes(analysis.scenes, transcript)
        return analysis

    def generate(
        self, analysis: BookAnalysis, out_dir: Path, dry_run: bool = False
    ) -> list[Frame]:
        gen_cfg = self.config.generation
        provider = get_provider(gen_cfg, dry_run=dry_run)
        frames_dir = out_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)

        scenes = analysis.scenes
        if gen_cfg.max_frames > 0:
            scenes = scenes[: gen_cfg.max_frames]
        self._progress(
            f"Generating {len(scenes)} frame(s) via "
            f"{'stub (dry-run)' if dry_run else provider.name}"
        )

        # Build prompts up front (cheap, deterministic).
        jobs = [
            (scene, build_prompt(scene, analysis, self.config.project.style))
            for scene in scenes
        ]

        frames: list[Frame] = []
        workers = max(1, gen_cfg.concurrency)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {
                pool.submit(self._render_one, provider, scene, prompt, frames_dir): scene
                for scene, prompt in jobs
            }
            done = 0
            for future in as_completed(future_map):
                frame = future.result()
                frames.append(frame)
                done += 1
                status = "ok" if frame.ok else f"FAILED ({frame.error})"
                self._progress(f"  [{done}/{len(jobs)}] {frame.scene_id}: {status}")

        # Restore scene order (futures complete out of order).
        order = {scene.id: i for i, scene in enumerate(scenes)}
        frames.sort(key=lambda f: order.get(f.scene_id, 1_000_000))
        return frames

    def _render_one(self, provider, scene, prompt: str, frames_dir: Path) -> Frame:
        frame = Frame(scene_id=scene.id, prompt=prompt, provider=provider.name, model=provider.model)
        try:
            path = provider.generate(prompt, frames_dir / scene.id)
            frame.image_path = str(path)
        except Exception as exc:  # noqa: BLE001 - one frame failing shouldn't abort
            frame.error = str(exc)
        return frame

    # -- full run -----------------------------------------------------------

    def run(
        self,
        ebook_path: Optional[str] = None,
        audio_path: Optional[str] = None,
        analyze_only: bool = False,
        dry_run: bool = False,
    ) -> BookAnalysis:
        out_dir = Path(self.config.output.dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        ingested = self.ingest(ebook_path, audio_path)

        # Persist the audiobook segmentation so each chapter/paragraph (with its
        # timestamps) can be inspected or processed individually.
        if ingested.structure is not None:
            structure_path = out_dir / "audiobook_structure.json"
            structure_path.write_text(
                ingested.structure.model_dump_json(indent=2), encoding="utf-8"
            )
            self._progress(f"Wrote audiobook structure -> {structure_path}")

        analysis = self.analyze(
            ingested.chapters, ingested.title, ingested.author, ingested.transcript
        )

        # Always persist the analysis so it can be inspected or reused.
        analysis_path = out_dir / "analysis.json"
        analysis_path.write_text(analysis.model_dump_json(indent=2), encoding="utf-8")
        self._progress(f"Wrote analysis -> {analysis_path}")

        if analyze_only:
            return analysis

        frames = self.generate(analysis, out_dir, dry_run=dry_run)

        manifest_path = out_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps([f.model_dump() for f in frames], indent=2), encoding="utf-8"
        )
        self._progress(f"Wrote manifest -> {manifest_path}")

        if self.config.output.gallery:
            gallery_path = render_gallery(analysis, frames, out_dir)
            self._progress(f"Wrote gallery -> {gallery_path}")

        return analysis
