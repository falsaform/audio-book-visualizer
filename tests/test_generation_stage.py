"""Exercise the generate + gallery stages end-to-end with the stub provider.

This covers the whole back half of the pipeline (prompt building, parallel
generation, manifest, gallery) without any network or API keys, by feeding in a
pre-built analysis instead of calling the Claude analyzer.
"""

from pathlib import Path

from audiobook_visualizer.config import Config
from audiobook_visualizer.gallery import render_gallery
from audiobook_visualizer.models import BookAnalysis, Character, Scene
from audiobook_visualizer.pipeline import Pipeline


def _analysis() -> BookAnalysis:
    return BookAnalysis(
        title="Test Book",
        author="Anon",
        characters=[Character(name="Ahab", description="grizzled captain with a white scar")],
        scenes=[
            Scene(
                id="scene_001_00",
                chapter="Chapter 1",
                title="The harbor at dawn",
                summary="Ishmael arrives at the docks.",
                visual_description="A grey harbor crowded with masts under a cold dawn.",
                setting="New Bedford docks",
                time_of_day="dawn",
                mood="melancholy",
            ),
            Scene(
                id="scene_002_00",
                chapter="Chapter 2",
                title="Ahab on the quarterdeck",
                summary="The captain paces the deck.",
                visual_description="A tall scarred captain strides the quarterdeck.",
                characters_present=["Ahab"],
                setting="ship deck",
                time_of_day="day",
                mood="ominous",
            ),
        ],
    )


def test_generate_and_gallery(tmp_path: Path):
    cfg = Config()
    cfg.output.dir = str(tmp_path)
    cfg.generation.concurrency = 2
    pipeline = Pipeline(cfg)

    frames = pipeline.generate(_analysis(), tmp_path, dry_run=True)

    assert len(frames) == 2
    assert all(f.ok for f in frames)
    assert all(Path(f.image_path).exists() for f in frames)
    # Order is preserved despite parallel execution.
    assert [f.scene_id for f in frames] == ["scene_001_00", "scene_002_00"]


def test_gallery_html_contains_scene_titles(tmp_path: Path):
    cfg = Config()
    pipeline = Pipeline(cfg)
    frames = pipeline.generate(_analysis(), tmp_path, dry_run=True)

    gallery = render_gallery(_analysis(), frames, tmp_path)
    html = gallery.read_text()
    assert "The harbor at dawn" in html
    assert "Ahab on the quarterdeck" in html
    assert "Test Book" in html
