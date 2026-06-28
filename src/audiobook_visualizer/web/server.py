"""FastAPI app for browsing and regenerating frames.

The server operates on an output directory from a prior ``abv visualize`` run.
The production database (``production.db``) lives at the *book* level; if the dir
it is pointed at is a per-chunk subfolder, the book dir is its parent. It shows
that segment's shots, serves the images, and exposes a regenerate endpoint that
re-renders a single shot through the pipeline (optionally with a hand-edited
prompt or style) and persists the result to the store.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from ..config import Config
from ..generation import build_prompt
from ..models import Frame
from ..pipeline import Pipeline
from ..store import ProductionStore
from .page import INDEX_HTML


class RegenerateRequest(BaseModel):
    # Defined at module scope (not inside create_app) so FastAPI can resolve the
    # annotation as a request body under `from __future__ import annotations`.
    prompt: Optional[str] = None
    style: Optional[str] = None


class UpdateShotRequest(BaseModel):
    visual_description: Optional[str] = None
    camera_move: Optional[str] = None
    shot_type: Optional[str] = None
    composition: Optional[str] = None
    subject: Optional[str] = None


class SplitShotRequest(BaseModel):
    at: Optional[float] = None  # split point as a fraction of the shot (0..1)


def _resolve_book_dir(out_dir: Path) -> tuple[Path, str]:
    """Return ``(book_dir, chunk_label)`` for the dir the server was pointed at."""
    if (out_dir / "production.db").exists():
        return out_dir, "full"
    if (out_dir.parent / "production.db").exists():
        return out_dir.parent, out_dir.name
    return out_dir, "full"  # no DB yet; routes will report an empty production


def create_app(out_dir: str | Path, config: Optional[Config] = None, dry_run: bool = False):
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles

    out_dir = Path(out_dir)
    config = config or Config.load()
    pipeline = Pipeline(config)

    book_dir, chunk_label = _resolve_book_dir(out_dir)
    store = ProductionStore.open(book_dir)

    frames_dir = out_dir / "frames"
    portraits_dir = book_dir / "portraits"
    frames_dir.mkdir(parents=True, exist_ok=True)
    portraits_dir.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="Audiobook Visualizer")
    app.mount("/frames", StaticFiles(directory=frames_dir), name="frames")
    app.mount("/portraits", StaticFiles(directory=portraits_dir), name="portraits")

    # -- helpers ------------------------------------------------------------

    def _ids() -> tuple[int, int]:
        pid = store.get_production_id()
        if pid is None:
            raise HTTPException(404, "No production found. Run `abv visualize` first.")
        seg_id = store.find_segment(pid, chunk_label)
        if seg_id is None:
            raise HTTPException(404, f"No segment '{chunk_label}' in the production.")
        return pid, seg_id

    def image_url(frame: Optional[Frame]) -> Optional[str]:
        if not frame or not frame.image_path:
            return None
        name = Path(frame.image_path).name
        fpath = frames_dir / name
        if not fpath.exists():
            return None
        return f"/frames/{name}?v={int(fpath.stat().st_mtime)}"

    def ref_urls(frame: Optional[Frame]) -> list[str]:
        urls = []
        for ref in (frame.references if frame else []) or []:
            name = Path(ref).name
            if (portraits_dir / name).exists():
                urls.append(f"/portraits/{name}")
        return urls

    def shot_payload(shot, analysis) -> dict:
        frame = shot.frame
        scene = shot.to_scene()
        # The effective prompt: the one that produced the current image, or — for an
        # unrendered/edited shot — the prompt that would be built from its fields.
        if frame and frame.prompt:
            prompt = frame.prompt
        else:
            prompt = build_prompt(
                scene, analysis, config.project.style,
                include_shot=config.generation.shot_variety,
            )
        return {
            **scene.model_dump(),
            "camera_move": shot.camera_move,
            "prompt": prompt,
            "frame": frame.model_dump() if frame else None,
            "image_url": image_url(frame),
            "reference_urls": ref_urls(frame),
        }

    def one_shot(pid: int, seg_id: int, slug: str) -> dict:
        analysis = store.load_segment_analysis(pid, seg_id)
        seg = store.segment_view(seg_id)
        shot = next((s for s in (seg.shots if seg else []) if s.slug == slug), None)
        if shot is None:
            raise HTTPException(404, f"Unknown shot: {slug}")
        return shot_payload(shot, analysis)

    # -- routes -------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return INDEX_HTML

    @app.get("/api/state")
    def state() -> dict:
        pid, seg_id = _ids()
        analysis = store.load_segment_analysis(pid, seg_id)
        view = store.production_view(pid)
        seg = store.segment_view(seg_id)
        shots = seg.shots if seg else []
        return {
            "title": view.title if view else "",
            "author": view.author if view else "",
            "provider": "stub (dry-run)" if dry_run else config.generation.provider,
            "style": config.project.style,
            "characters": [c.model_dump() for c in (view.characters if view else [])],
            "continuity": [
                {"severity": n.severity, "category": n.category, "message": n.message}
                for n in store.list_continuity_notes(seg_id)
            ],
            "scenes": [shot_payload(s, analysis) for s in shots],
        }

    @app.post("/api/scenes/{scene_id}/regenerate")
    def regenerate(scene_id: str, req: RegenerateRequest) -> dict:
        pid, seg_id = _ids()
        analysis = store.load_segment_analysis(pid, seg_id)
        try:
            frame: Frame = pipeline.regenerate_frame(
                analysis, out_dir, store, pid, seg_id, scene_id,
                prompt_override=req.prompt or None,
                style_override=req.style or None,
                dry_run=dry_run,
            )
        except KeyError:
            raise HTTPException(404, f"Unknown scene: {scene_id}")
        if not frame.ok:
            raise HTTPException(502, frame.error or "Image generation failed.")
        return one_shot(pid, seg_id, scene_id)

    @app.post("/api/shots/{slug}/update")
    def update_shot(slug: str, req: UpdateShotRequest) -> dict:
        pid, seg_id = _ids()
        if not store.update_shot(seg_id, slug, req.model_dump(exclude_none=True)):
            raise HTTPException(404, f"Unknown shot: {slug}")
        return one_shot(pid, seg_id, slug)

    @app.post("/api/shots/{slug}/split")
    def split_shot(slug: str, req: SplitShotRequest) -> dict:
        pid, seg_id = _ids()
        result = store.split_shot(seg_id, slug, req.at if req.at is not None else 0.5)
        if result is None:
            raise HTTPException(404, f"Unknown shot: {slug}")
        return {"shots": [one_shot(pid, seg_id, s) for s in result]}

    return app
