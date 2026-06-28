"""FastAPI app for browsing and regenerating frames.

The server operates on an existing output directory (from a prior ``abv
visualize``/``--analyze-only`` run): it reads ``analysis.json`` and
``manifest.json``, serves the images, and exposes a regenerate endpoint that
re-renders a single scene through the pipeline (with an optional hand-edited
prompt or style) and updates the manifest.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from ..config import Config
from ..models import BookAnalysis, Frame
from ..pipeline import Pipeline
from .page import INDEX_HTML


class RegenerateRequest(BaseModel):
    # Defined at module scope (not inside create_app) so FastAPI can resolve the
    # annotation as a request body under `from __future__ import annotations`.
    prompt: Optional[str] = None
    style: Optional[str] = None


def create_app(out_dir: str | Path, config: Optional[Config] = None, dry_run: bool = False):
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles

    out_dir = Path(out_dir)
    config = config or Config.load()
    pipeline = Pipeline(config)

    frames_dir = out_dir / "frames"
    portraits_dir = out_dir / "portraits"
    frames_dir.mkdir(parents=True, exist_ok=True)
    portraits_dir.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="Audiobook Visualizer")
    app.mount("/frames", StaticFiles(directory=frames_dir), name="frames")
    app.mount("/portraits", StaticFiles(directory=portraits_dir), name="portraits")

    # -- helpers ------------------------------------------------------------

    def load_analysis() -> BookAnalysis:
        path = out_dir / "analysis.json"
        if not path.exists():
            raise HTTPException(
                404,
                "No analysis.json in the output dir. Run "
                "`abv visualize --analyze-only` (or a full run) first.",
            )
        return BookAnalysis.model_validate_json(path.read_text())

    def load_manifest() -> dict[str, dict]:
        path = out_dir / "manifest.json"
        if not path.exists():
            return {}
        try:
            return {f["scene_id"]: f for f in json.loads(path.read_text())}
        except (json.JSONDecodeError, KeyError, TypeError):
            return {}

    def save_manifest(manifest: dict[str, dict], analysis: BookAnalysis) -> None:
        order = {s.id: i for i, s in enumerate(analysis.scenes)}
        rows = sorted(manifest.values(), key=lambda f: order.get(f["scene_id"], 1 << 30))
        (out_dir / "manifest.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")

    def image_url(frame: Optional[dict]) -> Optional[str]:
        if not frame or not frame.get("image_path"):
            return None
        name = Path(frame["image_path"]).name
        fpath = frames_dir / name
        if not fpath.exists():
            return None
        return f"/frames/{name}?v={int(fpath.stat().st_mtime)}"

    def ref_urls(frame: Optional[dict]) -> list[str]:
        urls = []
        for ref in (frame or {}).get("references", []) or []:
            name = Path(ref).name
            if (portraits_dir / name).exists():
                urls.append(f"/portraits/{name}")
        return urls

    def scene_payload(scene, manifest) -> dict:
        frame = manifest.get(scene.id)
        return {
            **scene.model_dump(),
            "frame": frame,
            "image_url": image_url(frame),
            "reference_urls": ref_urls(frame),
        }

    # -- routes -------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return INDEX_HTML

    @app.get("/api/state")
    def state() -> dict:
        analysis = load_analysis()
        manifest = load_manifest()
        return {
            "title": analysis.title,
            "author": analysis.author,
            "provider": "stub (dry-run)" if dry_run else config.generation.provider,
            "style": config.project.style,
            "characters": [c.model_dump() for c in analysis.characters],
            "scenes": [scene_payload(s, manifest) for s in analysis.scenes],
        }

    @app.post("/api/scenes/{scene_id}/regenerate")
    def regenerate(scene_id: str, req: RegenerateRequest) -> dict:
        analysis = load_analysis()
        try:
            frame: Frame = pipeline.regenerate_frame(
                analysis, out_dir, scene_id,
                prompt_override=req.prompt or None,
                style_override=req.style or None,
                dry_run=dry_run,
            )
        except KeyError:
            raise HTTPException(404, f"Unknown scene: {scene_id}")
        if not frame.ok:
            raise HTTPException(502, frame.error or "Image generation failed.")

        manifest = load_manifest()
        manifest[scene_id] = frame.model_dump()
        save_manifest(manifest, analysis)

        scene = next(s for s in analysis.scenes if s.id == scene_id)
        return scene_payload(scene, manifest)

    return app
