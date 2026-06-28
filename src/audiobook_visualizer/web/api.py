"""The JSON API (Django Ninja).

``django_setup()`` runs before importing ``ninja`` because django-ninja reads
Django settings at import time. Routes are registered on a per-:class:`Studio`
``NinjaAPI`` so the same module can serve different output dirs / tests.
"""

from __future__ import annotations

from typing import Optional

from .django_conf import django_setup

django_setup()

from ninja import NinjaAPI, Schema  # noqa: E402

from .studio import NotFound, Studio  # noqa: E402


class FrameOut(Schema):
    scene_id: str
    prompt: str = ""
    image_path: Optional[str] = None
    provider: str = ""
    model: str = ""
    references: list[str] = []
    cached: bool = False
    error: Optional[str] = None


class ShotOut(Schema):
    id: str
    chapter: Optional[str] = None
    title: str = ""
    summary: str = ""
    setting: str = ""
    time_of_day: str = ""
    mood: str = ""
    shot_type: str = ""
    characters_present: list[str] = []
    visual_description: str = ""
    source_excerpt: str = ""
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    camera_move: str = ""
    prompt: str = ""
    frame: Optional[FrameOut] = None
    image_url: Optional[str] = None
    reference_urls: list[str] = []


class CharacterOut(Schema):
    name: str
    aliases: list[str] = []
    description: str = ""
    role: str = ""


class ContinuityOut(Schema):
    severity: str
    category: str
    message: str


class JobOut(Schema):
    id: int
    shot_slug: str
    status: str
    error: Optional[str] = None
    image_url: Optional[str] = None


class StateOut(Schema):
    title: str = ""
    author: str = ""
    provider: str = ""
    style: str = ""
    characters: list[CharacterOut] = []
    continuity: list[ContinuityOut] = []
    jobs: list[JobOut] = []
    scenes: list[ShotOut] = []


class SplitOut(Schema):
    shots: list[ShotOut]


class RenderIn(Schema):
    prompt: Optional[str] = None
    style: Optional[str] = None


class UpdateShotIn(Schema):
    visual_description: Optional[str] = None
    camera_move: Optional[str] = None
    shot_type: Optional[str] = None
    composition: Optional[str] = None
    subject: Optional[str] = None


class SplitShotIn(Schema):
    at: Optional[float] = None


def build_api(studio: Studio) -> NinjaAPI:
    api = NinjaAPI(title="Audiobook Visualizer", version="1.0.0", urls_namespace="abv")

    @api.exception_handler(NotFound)
    def _not_found(request, exc):  # noqa: ANN001
        return api.create_response(request, {"detail": str(exc)}, status=404)

    @api.get("/state", response=StateOut, operation_id="getState")
    def state(request):  # noqa: ANN001
        return studio.state()

    @api.post("/shots/{slug}/render", response=JobOut, operation_id="renderShot")
    def render(request, slug: str, body: RenderIn):  # noqa: ANN001
        """Enqueue an async re-render; returns the queued job (poll /jobs/{id})."""
        return studio.enqueue_render(slug, body.prompt or None, body.style or None)

    @api.get("/jobs/{job_id}", response=JobOut, operation_id="getJob")
    def job(request, job_id: int):  # noqa: ANN001
        return studio.job(job_id)

    @api.post("/shots/{slug}/update", response=ShotOut, operation_id="updateShot")
    def update_shot(request, slug: str, body: UpdateShotIn):  # noqa: ANN001
        return studio.update_shot(slug, body.dict(exclude_none=True))

    @api.post("/shots/{slug}/split", response=SplitOut, operation_id="splitShot")
    def split_shot(request, slug: str, body: SplitShotIn):  # noqa: ANN001
        return studio.split_shot(slug, body.at if body.at is not None else 0.5)

    return api


def openapi_schema() -> dict:
    """The OpenAPI document (for the frontend's Kubb codegen). Routes don't touch
    the studio at schema-build time, so a throwaway one is fine."""
    import tempfile

    from ..config import Config

    api = build_api(Studio(tempfile.mkdtemp(), Config(), dry_run=True))
    # Empty prefix: the schema needs no mounted urlconf (avoids reverse()), and the
    # frontend client adds the `/api` baseURL itself (no double prefix).
    return api.get_openapi_schema(path_prefix="")


if __name__ == "__main__":  # `python -m audiobook_visualizer.web.api > openapi.json`
    import json
    import sys

    json.dump(openapi_schema(), sys.stdout, indent=2)
