"""Tests for the web UI server (offline: stub provider via dry_run)."""

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from audiobook_visualizer.config import Config  # noqa: E402
from audiobook_visualizer.models import BookAnalysis, Character, Scene  # noqa: E402
from audiobook_visualizer.web.server import create_app  # noqa: E402


def _write_analysis(out_dir: Path) -> BookAnalysis:
    analysis = BookAnalysis(
        title="Test Book",
        author="Anon",
        characters=[Character(name="Ahab", description="grizzled captain, white scar")],
        scenes=[
            Scene(id="scene_001_00", title="Harbor", chapter="Chapter 1",
                  visual_description="grey harbor at dawn", shot_type="wide shot"),
            Scene(id="scene_002_00", title="On deck", visual_description="captain at the helm",
                  characters_present=["Ahab"]),
        ],
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "analysis.json").write_text(analysis.model_dump_json(indent=2))
    return analysis


def _client(out_dir: Path) -> TestClient:
    cfg = Config()
    cfg.output.dir = str(out_dir)
    return TestClient(create_app(out_dir, cfg, dry_run=True))


def test_index_serves_html(tmp_path: Path):
    _write_analysis(tmp_path)
    res = _client(tmp_path).get("/")
    assert res.status_code == 200
    assert "Audiobook Visualizer" in res.text
    assert "/api/state" in res.text  # the SPA wires to the API


def test_state_lists_scenes(tmp_path: Path):
    _write_analysis(tmp_path)
    data = _client(tmp_path).get("/api/state").json()
    assert data["title"] == "Test Book"
    assert data["provider"] == "stub (dry-run)"
    assert [s["id"] for s in data["scenes"]] == ["scene_001_00", "scene_002_00"]
    assert all(s["image_url"] is None for s in data["scenes"])  # nothing rendered yet


def test_state_requires_analysis(tmp_path: Path):
    # No analysis.json written.
    res = _client(tmp_path).get("/api/state")
    assert res.status_code == 404


def test_regenerate_creates_frame_and_updates_manifest(tmp_path: Path):
    _write_analysis(tmp_path)
    client = _client(tmp_path)

    res = client.post("/api/scenes/scene_001_00/regenerate", json={})
    assert res.status_code == 200
    payload = res.json()
    assert payload["image_url"] and payload["image_url"].startswith("/frames/scene_001_00.png")
    assert (tmp_path / "frames" / "scene_001_00.png").exists()

    # Manifest persisted with the new frame.
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest[0]["scene_id"] == "scene_001_00"

    # And it now shows up in state with an image URL.
    state = client.get("/api/state").json()
    first = next(s for s in state["scenes"] if s["id"] == "scene_001_00")
    assert first["image_url"] is not None


def test_regenerate_honors_prompt_override(tmp_path: Path):
    _write_analysis(tmp_path)
    client = _client(tmp_path)
    res = client.post(
        "/api/scenes/scene_002_00/regenerate",
        json={"prompt": "a totally custom prompt"},
    )
    assert res.status_code == 200
    assert res.json()["frame"]["prompt"] == "a totally custom prompt"


def test_regenerate_unknown_scene_404(tmp_path: Path):
    _write_analysis(tmp_path)
    res = _client(tmp_path).post("/api/scenes/nope/regenerate", json={})
    assert res.status_code == 404
