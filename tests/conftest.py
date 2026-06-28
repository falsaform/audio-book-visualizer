"""Shared test helpers for seeding and reading the production store.

``store_seeder`` is the DB analogue of the old on-disk ``analysis.json`` /
``manifest.json`` fixtures: it writes a production with segments, shots and
(optionally) rendered frame PNGs, so tests that used to scaffold JSON files now
scaffold the store instead.
"""

from pathlib import Path

import pytest

from audiobook_visualizer.models import Scene
from audiobook_visualizer.models import Frame as FrameDTO
from audiobook_visualizer.store import ProductionStore


@pytest.fixture
def store_seeder():
    def _seed(book_dir, segments, *, title="Book", author="", characters=None):
        """Seed a production.

        ``segments``: list of ``{"label", "start", "end", "scenes": [...]}`` where
        each scene is ``{"slug", "start", "end", "title"?, "chapter"?, "desc"?,
        "characters"?, "frame"?(bool), "cache_key"?}``. ``frame=True`` writes a PNG
        under ``<chunk>/frames/<slug>.png`` and a Frame row.
        """
        book_dir = Path(book_dir)
        store = ProductionStore.open(book_dir)
        pid = store.get_or_create_production(title=title, author=author)
        if characters:
            store.upsert_characters(pid, characters)
        for seg in segments:
            label = seg["label"]
            chunk_dir = book_dir if label == "full" else book_dir / label
            scenes = [
                Scene(
                    id=sc["slug"], title=sc.get("title", sc["slug"]),
                    chapter=sc.get("chapter"), summary=sc.get("summary", ""),
                    visual_description=sc.get("desc", ""),
                    characters_present=sc.get("characters", []),
                    start_time=sc.get("start"), end_time=sc.get("end"),
                )
                for sc in seg["scenes"]
            ]
            sid = store.get_or_create_segment(pid, label, seg.get("start", 0.0), seg.get("end", 0.0))
            store.persist_scenes_as_shots(pid, sid, scenes)
            for sc in seg["scenes"]:
                if sc.get("frame"):
                    frames_dir = chunk_dir / "frames"
                    frames_dir.mkdir(parents=True, exist_ok=True)
                    img = frames_dir / f"{sc['slug']}.png"
                    img.write_bytes(b"\x89PNG")
                    store.upsert_frame(
                        sid,
                        FrameDTO(scene_id=sc["slug"], prompt="p", image_path=str(img),
                                 provider="stub", model="m"),
                        cache_key=sc.get("cache_key"),
                    )
        return store, pid

    return _seed


@pytest.fixture
def book_view():
    def _view(book_dir):
        store = ProductionStore.open(Path(book_dir))
        pid = store.get_production_id()
        return store.production_view(pid) if pid else None

    return _view
