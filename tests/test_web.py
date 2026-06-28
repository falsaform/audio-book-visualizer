"""Tests for the web UI server (offline: stub provider via dry_run)."""

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from audiobook_visualizer.config import Config  # noqa: E402
from audiobook_visualizer.models import Character  # noqa: E402
from audiobook_visualizer.store import ProductionStore  # noqa: E402
from audiobook_visualizer.web.server import create_app  # noqa: E402


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
    cfg = Config()
    cfg.output.dir = str(out_dir)
    return TestClient(create_app(out_dir, cfg, dry_run=True))


def test_index_serves_html(tmp_path: Path, store_seeder):
    _seed(store_seeder, tmp_path)
    res = _client(tmp_path).get("/")
    assert res.status_code == 200
    assert "Audiobook Visualizer" in res.text
    assert "/api/state" in res.text  # the SPA wires to the API


def test_state_lists_scenes(tmp_path: Path, store_seeder):
    _seed(store_seeder, tmp_path)
    data = _client(tmp_path).get("/api/state").json()
    assert data["title"] == "Test Book"
    assert data["provider"] == "stub (dry-run)"
    assert [s["id"] for s in data["scenes"]] == ["scene_001_00", "scene_002_00"]
    assert all(s["image_url"] is None for s in data["scenes"])  # nothing rendered yet


def test_state_requires_analysis(tmp_path: Path):
    # No production seeded.
    res = _client(tmp_path).get("/api/state")
    assert res.status_code == 404


def test_regenerate_creates_frame_and_persists(tmp_path: Path, store_seeder):
    _seed(store_seeder, tmp_path)
    client = _client(tmp_path)

    res = client.post("/api/scenes/scene_001_00/regenerate", json={})
    assert res.status_code == 200
    payload = res.json()
    assert payload["image_url"] and payload["image_url"].startswith("/frames/scene_001_00.png")
    assert (tmp_path / "frames" / "scene_001_00.png").exists()

    # Frame persisted to the production store.
    store = ProductionStore.open(tmp_path)
    pid = store.get_production_id()
    seg_id = store.find_segment(pid, "full")
    frame = store.get_frame(seg_id, "scene_001_00")
    assert frame is not None and frame.image_path is not None

    # And it now shows up in state with an image URL.
    state = client.get("/api/state").json()
    first = next(s for s in state["scenes"] if s["id"] == "scene_001_00")
    assert first["image_url"] is not None


def test_regenerate_honors_prompt_override(tmp_path: Path, store_seeder):
    _seed(store_seeder, tmp_path)
    client = _client(tmp_path)
    res = client.post(
        "/api/scenes/scene_002_00/regenerate",
        json={"prompt": "a totally custom prompt"},
    )
    assert res.status_code == 200
    assert res.json()["frame"]["prompt"] == "a totally custom prompt"


def test_regenerate_unknown_scene_404(tmp_path: Path, store_seeder):
    _seed(store_seeder, tmp_path)
    res = _client(tmp_path).post("/api/scenes/nope/regenerate", json={})
    assert res.status_code == 404


def test_state_includes_prompt_and_source(tmp_path: Path, store_seeder):
    _seed(store_seeder, tmp_path)
    state = _client(tmp_path).get("/api/state").json()
    # Every shot exposes its (effective) prompt and source fields for the editor.
    assert all("prompt" in s and s["prompt"] for s in state["scenes"])
    assert all("source_excerpt" in s and "camera_move" in s for s in state["scenes"])


def test_update_shot_edits_fields(tmp_path: Path, store_seeder):
    _seed(store_seeder, tmp_path)
    client = _client(tmp_path)
    res = client.post("/api/shots/scene_001_00/update",
                      json={"visual_description": "a new look", "camera_move": "push_in",
                            "shot_type": "close-up"})
    assert res.status_code == 200
    data = res.json()
    assert data["visual_description"] == "a new look"
    assert data["camera_move"] == "push_in" and data["shot_type"] == "close-up"
    # Persisted: a fresh state reflects it.
    state = client.get("/api/state").json()
    sc = next(s for s in state["scenes"] if s["id"] == "scene_001_00")
    assert sc["camera_move"] == "push_in"


def test_split_shot_adds_a_shot(tmp_path: Path, store_seeder):
    _seed(store_seeder, tmp_path)
    client = _client(tmp_path)
    before = len(client.get("/api/state").json()["scenes"])
    res = client.post("/api/shots/scene_001_00/split", json={"at": 0.5})
    assert res.status_code == 200
    pair = res.json()["shots"]
    assert [p["id"] for p in pair] == ["scene_001_00", "scene_001_00-2"]
    after = client.get("/api/state").json()["scenes"]
    assert len(after) == before + 1
    assert any(s["id"] == "scene_001_00-2" for s in after)


def test_update_unknown_shot_404(tmp_path: Path, store_seeder):
    _seed(store_seeder, tmp_path)
    res = _client(tmp_path).post("/api/shots/nope/update", json={"camera_move": "static"})
    assert res.status_code == 404
