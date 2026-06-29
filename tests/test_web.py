"""Tests for the web API (Django Ninja; offline stub provider via dry_run).

Uses Ninja's TestClient against the per-Studio API. Render jobs run inline
(``sync=True``) so the async queue is exercised deterministically.
"""

from pathlib import Path

import importlib.util

import pytest

if importlib.util.find_spec("django") is None or importlib.util.find_spec("ninja") is None:
    pytest.skip("web extras not installed", allow_module_level=True)

# django-ninja reads settings at import time, so importing the api module (which
# calls django_setup() first) must happen before any other ninja import.
from audiobook_visualizer.web.api import build_api  # noqa: E402
from audiobook_visualizer.web.studio import Studio  # noqa: E402
from ninja.testing import TestClient  # noqa: E402

from audiobook_visualizer.config import Config  # noqa: E402
from audiobook_visualizer.models import Character  # noqa: E402


def _seed(seeder, out_dir: Path) -> None:
    seeder(
        out_dir,
        [{
            "label": "full", "start": 0.0, "end": 120.0,
            "scenes": [
                {"slug": "scene_001_00", "title": "Harbor", "chapter": "Chapter 1",
                 "desc": "grey harbor at dawn", "start": 0.0, "end": 60.0},
                {"slug": "scene_002_00", "title": "On deck", "desc": "captain at the helm",
                 "characters": ["Ahab"], "start": 60.0, "end": 120.0},
            ],
        }],
        title="Test Book", author="Anon",
        characters=[Character(name="Ahab", description="grizzled captain, white scar")],
    )


def _client(out_dir: Path) -> TestClient:
    studio = Studio(out_dir, Config(), dry_run=True, sync=True)
    return TestClient(build_api(studio))


def test_state_lists_shots(tmp_path, store_seeder):
    _seed(store_seeder, tmp_path)
    data = _client(tmp_path).get("/state").json()
    assert data["title"] == "Test Book"
    assert data["provider"] == "stub (dry-run)"
    assert [s["id"] for s in data["scenes"]] == ["scene_001_00", "scene_002_00"]
    assert all(s["image_url"] is None for s in data["scenes"])  # nothing rendered yet


def test_state_includes_prompt_source_and_jobs(tmp_path, store_seeder):
    _seed(store_seeder, tmp_path)
    data = _client(tmp_path).get("/state").json()
    assert all(s["prompt"] and "source_excerpt" in s and "camera_move" in s for s in data["scenes"])
    assert data["jobs"] == []  # nothing queued yet


def test_state_requires_production(tmp_path):
    res = _client(tmp_path).get("/state")
    assert res.status_code == 404


def test_render_enqueues_and_completes(tmp_path, store_seeder):
    _seed(store_seeder, tmp_path)
    client = _client(tmp_path)

    job = client.post("/shots/scene_001_00/render", json={}).json()
    assert job["status"] == "done"  # sync mode completes inline
    assert job["image_url"] and job["image_url"].startswith("/frames/scene_001_00.png")
    assert (tmp_path / "frames" / "scene_001_00.png").exists()

    # The job is pollable and the shot now shows an image in state.
    polled = client.get(f"/jobs/{job['id']}").json()
    assert polled["status"] == "done"
    state = client.get("/state").json()
    first = next(s for s in state["scenes"] if s["id"] == "scene_001_00")
    assert first["image_url"] is not None


def test_render_honors_prompt_override(tmp_path, store_seeder):
    _seed(store_seeder, tmp_path)
    client = _client(tmp_path)
    job = client.post("/shots/scene_002_00/render",
                      json={"prompt": "a totally custom prompt"}).json()
    assert job["status"] == "done"
    # The override is the prompt the shot now reports.
    state = client.get("/state").json()
    shot = next(s for s in state["scenes"] if s["id"] == "scene_002_00")
    assert shot["frame"]["prompt"] == "a totally custom prompt"


def test_render_unknown_shot_404(tmp_path, store_seeder):
    _seed(store_seeder, tmp_path)
    assert _client(tmp_path).post("/shots/nope/render", json={}).status_code == 404


def test_update_shot_edits_fields(tmp_path, store_seeder):
    _seed(store_seeder, tmp_path)
    client = _client(tmp_path)
    data = client.post("/shots/scene_001_00/update",
                       json={"visual_description": "a new look", "camera_move": "push_in",
                             "shot_type": "close-up"}).json()
    assert data["visual_description"] == "a new look"
    assert data["camera_move"] == "push_in" and data["shot_type"] == "close-up"
    state = client.get("/state").json()
    assert next(s for s in state["scenes"] if s["id"] == "scene_001_00")["camera_move"] == "push_in"


def test_split_shot_adds_a_shot(tmp_path, store_seeder):
    _seed(store_seeder, tmp_path)
    client = _client(tmp_path)
    before = len(client.get("/state").json()["scenes"])
    pair = client.post("/shots/scene_001_00/split", json={"at": 0.5}).json()["shots"]
    assert [p["id"] for p in pair] == ["scene_001_00", "scene_001_00-2"]
    after = client.get("/state").json()["scenes"]
    assert len(after) == before + 1


def test_update_unknown_shot_404(tmp_path, store_seeder):
    _seed(store_seeder, tmp_path)
    assert _client(tmp_path).post("/shots/nope/update", json={"camera_move": "static"}).status_code == 404


def test_render_all_generates_unrendered(tmp_path, store_seeder):
    _seed(store_seeder, tmp_path)
    client = _client(tmp_path)

    # Review state first: shots planned but not rendered (the review-before-generate flow).
    state = client.get("/state").json()
    assert all(not s["rendered"] for s in state["scenes"])
    assert all("scene_heading" in s for s in state["scenes"])

    res = client.post("/render-all", json={"only_missing": True}).json()
    assert res["queued"] == 2 and all(j["status"] == "done" for j in res["jobs"])  # sync mode
    assert all(s["rendered"] for s in client.get("/state").json()["scenes"])

    # Nothing left to do on a second pass.
    assert client.post("/render-all", json={"only_missing": True}).json()["queued"] == 0
