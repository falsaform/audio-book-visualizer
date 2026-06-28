"""The web layer's runtime context: the production store, the render pipeline,
and an in-process render-job worker.

A re-render is asynchronous: the API enqueues a :class:`RenderJob` (persisted in
``production.db``) and submits it to a small thread pool; the frontend polls the
job until it finishes. No external broker/process is involved.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from ..config import Config
from ..generation import build_prompt
from ..pipeline import Pipeline
from ..store import JobView, ProductionStore


def resolve_book_dir(out_dir: Path) -> tuple[Path, str]:
    """``(book_dir, chunk_label)`` for the dir the server was pointed at."""
    if (out_dir / "production.db").exists():
        return out_dir, "full"
    if (out_dir.parent / "production.db").exists():
        return out_dir.parent, out_dir.name
    return out_dir, "full"


def frontend_dist() -> Optional[Path]:
    """The built Vite SPA, if present (``frontend/dist`` next to the repo)."""
    here = Path(__file__).resolve()
    for root in here.parents:
        cand = root / "frontend" / "dist"
        if (cand / "index.html").exists():
            return cand
        if (root / "pyproject.toml").exists():
            break
    return None


class NotFound(Exception):
    """Raised by the studio; the API maps it to a 404."""


class Studio:
    def __init__(self, out_dir: str | Path, config: Optional[Config] = None,
                 dry_run: bool = False, sync: bool = False) -> None:
        self.out_dir = Path(out_dir)
        self.config = config or Config.load()
        self.dry_run = dry_run
        self.sync = sync  # run jobs inline (tests) instead of on the thread pool
        self.book_dir, self.chunk_label = resolve_book_dir(self.out_dir)
        self.store = ProductionStore.open(self.book_dir)
        self.pipeline = Pipeline(self.config)
        self.frames_dir = self.out_dir / "frames"
        self.portraits_dir = self.book_dir / "portraits"
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        self.portraits_dir.mkdir(parents=True, exist_ok=True)
        self.dist_dir = frontend_dist()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="abv-render")

    # -- ids ----------------------------------------------------------------

    def ids(self) -> tuple[int, int]:
        pid = self.store.get_production_id()
        if pid is None:
            raise NotFound("No production found. Run `abv visualize` first.")
        seg_id = self.store.find_segment(pid, self.chunk_label)
        if seg_id is None:
            raise NotFound(f"No segment '{self.chunk_label}' in the production.")
        return pid, seg_id

    # -- payloads -----------------------------------------------------------

    def image_url(self, image_path: Optional[str]) -> Optional[str]:
        if not image_path:
            return None
        name = Path(image_path).name
        fpath = self.frames_dir / name
        if not fpath.exists():
            return None
        return f"/frames/{name}?v={int(fpath.stat().st_mtime)}"

    def _ref_urls(self, frame) -> list[str]:
        urls = []
        for ref in (frame.references if frame else []) or []:
            name = Path(ref).name
            if (self.portraits_dir / name).exists():
                urls.append(f"/portraits/{name}")
        return urls

    def shot_payload(self, shot, analysis) -> dict:
        frame = shot.frame
        scene = shot.to_scene()
        if frame and frame.prompt:
            prompt = frame.prompt
        else:
            prompt = build_prompt(
                scene, analysis, self.config.project.style,
                include_shot=self.config.generation.shot_variety,
            )
        return {
            **scene.model_dump(),
            "camera_move": shot.camera_move,
            "prompt": prompt,
            "frame": frame.model_dump() if frame else None,
            "image_url": self.image_url(frame.image_path if frame else None),
            "reference_urls": self._ref_urls(frame),
        }

    def one_shot(self, pid: int, seg_id: int, slug: str) -> dict:
        analysis = self.store.load_segment_analysis(pid, seg_id)
        seg = self.store.segment_view(seg_id)
        shot = next((s for s in (seg.shots if seg else []) if s.slug == slug), None)
        if shot is None:
            raise NotFound(f"Unknown shot: {slug}")
        return self.shot_payload(shot, analysis)

    def state(self) -> dict:
        pid, seg_id = self.ids()
        analysis = self.store.load_segment_analysis(pid, seg_id)
        view = self.store.production_view(pid)
        seg = self.store.segment_view(seg_id)
        shots = seg.shots if seg else []
        return {
            "title": view.title if view else "",
            "author": view.author if view else "",
            "provider": "stub (dry-run)" if self.dry_run else self.config.generation.provider,
            "style": self.config.project.style,
            "characters": [c.model_dump() for c in (view.characters if view else [])],
            "continuity": [
                {"severity": n.severity, "category": n.category, "message": n.message}
                for n in self.store.list_continuity_notes(seg_id)
            ],
            "jobs": [self._job_dict(j) for j in self.store.list_render_jobs(seg_id, active_only=True)],
            "scenes": [self.shot_payload(s, analysis) for s in shots],
        }

    # -- shot edits ---------------------------------------------------------

    def update_shot(self, slug: str, fields: dict) -> dict:
        pid, seg_id = self.ids()
        if not self.store.update_shot(seg_id, slug, fields):
            raise NotFound(f"Unknown shot: {slug}")
        return self.one_shot(pid, seg_id, slug)

    def split_shot(self, slug: str, at: float) -> dict:
        pid, seg_id = self.ids()
        result = self.store.split_shot(seg_id, slug, at)
        if result is None:
            raise NotFound(f"Unknown shot: {slug}")
        return {"shots": [self.one_shot(pid, seg_id, s) for s in result]}

    # -- async render queue -------------------------------------------------

    def enqueue_render(self, slug: str, prompt: Optional[str], style: Optional[str]) -> dict:
        pid, seg_id = self.ids()
        seg = self.store.segment_view(seg_id)
        if not any(s.slug == slug for s in (seg.shots if seg else [])):
            raise NotFound(f"Unknown shot: {slug}")
        job_id = self.store.create_render_job(seg_id, slug, time.time(), prompt or None, style or None)
        if self.sync:
            self._run_job(job_id)
        else:
            self._executor.submit(self._run_job, job_id)
        return self.job(job_id)

    def job(self, job_id: int) -> dict:
        view = self.store.get_render_job(job_id)
        if view is None:
            raise NotFound(f"Unknown job: {job_id}")
        return self._job_dict(view)

    def _job_dict(self, view: JobView) -> dict:
        return {
            "id": view.id, "shot_slug": view.shot_slug, "status": view.status,
            "error": view.error, "image_url": self.image_url(view.image_path),
        }

    def _run_job(self, job_id: int) -> None:
        req = self.store.get_render_job_request(job_id)
        if req is None:
            return
        seg_id, slug, prompt, style = req
        self.store.update_render_job(job_id, "running", time.time())
        try:
            pid = self.store.get_production_id()
            analysis = self.store.load_segment_analysis(pid, seg_id)
            frame = self.pipeline.regenerate_frame(
                analysis, self.out_dir, self.store, pid, seg_id, slug,
                prompt_override=prompt, style_override=style, dry_run=self.dry_run,
            )
            if frame.ok:
                self.store.update_render_job(job_id, "done", time.time(), image_path=frame.image_path)
            else:
                self.store.update_render_job(
                    job_id, "error", time.time(), error=frame.error or "render failed")
        except Exception as exc:  # noqa: BLE001 - surface the error to the client
            self.store.update_render_job(job_id, "error", time.time(), error=str(exc))
