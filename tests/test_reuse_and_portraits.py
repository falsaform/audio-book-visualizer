"""Reuse-analysis, delete-to-regenerate, and square portraits."""

from pathlib import Path

import pytest

import audiobook_visualizer.pipeline as pmod
from audiobook_visualizer.config import Config
from audiobook_visualizer.models import BookAnalysis, Character, Scene
from audiobook_visualizer.pipeline import Pipeline


def test_portrait_size_default_is_square():
    assert Config().generation.portrait_size == "1024x1024"


def test_portrait_rendered_square_not_widescreen(tmp_path):
    from PIL import Image

    analysis = BookAnalysis(
        characters=[Character(name="Bob", description="a tall man in a grey coat")],
        scenes=[Scene(id="s1", visual_description="a man", characters_present=["Bob"])],
    )
    Pipeline(Config()).generate(analysis, tmp_path, dry_run=True)

    portraits = list((tmp_path / "portraits").glob("*.png"))
    assert portraits
    # Square (1024x1024), not the widescreen frame size — no cut-off face.
    assert Image.open(portraits[0]).size == (1024, 1024)


def test_frame_kept_until_deleted(tmp_path):
    analysis = BookAnalysis(scenes=[Scene(id="s1", visual_description="x")])
    pipeline = Pipeline(Config())

    first = pipeline.generate(analysis, tmp_path, dry_run=True)
    assert not first[0].cached

    second = pipeline.generate(analysis, tmp_path, dry_run=True)
    assert second[0].cached  # file exists -> skipped

    # Delete the frame -> it regenerates; others untouched.
    Path(second[0].image_path).unlink()
    third = pipeline.generate(analysis, tmp_path, dry_run=True)
    assert not third[0].cached


def test_force_regenerates_existing(tmp_path):
    analysis = BookAnalysis(scenes=[Scene(id="s1", visual_description="x")])
    pipeline = Pipeline(Config())
    pipeline.generate(analysis, tmp_path, dry_run=True)
    forced = pipeline.generate(analysis, tmp_path, dry_run=True, force=True)
    assert not forced[0].cached


def test_run_reuses_existing_analysis(tmp_path, monkeypatch):
    monkeypatch.setattr(
        pmod, "transcribe_audio",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not transcribe")),
    )
    book = tmp_path / "legion"
    chunk = book / "0000-5min"
    chunk.mkdir(parents=True)
    analysis = BookAnalysis(
        title="Legion",
        scenes=[Scene(id="0000-5min_001_00", visual_description="a scene")],
    )
    (chunk / "analysis.json").write_text(analysis.model_dump_json())

    cfg = Config()
    cfg.output.dir = str(book)
    result = Pipeline(cfg).run(
        structure_path="any/audiobook_structure_0000-5min.json", dry_run=True
    )

    assert result.title == "Legion"
    # Generation ran from the reused analysis (no transcription).
    assert (chunk / "frames" / "0000-5min_001_00.png").exists()


def test_reanalyze_bypasses_reuse(tmp_path):
    book = tmp_path / "legion"
    chunk = book / "0000-5min"
    chunk.mkdir(parents=True)
    (chunk / "analysis.json").write_text(BookAnalysis(title="X").model_dump_json())

    cfg = Config()
    cfg.output.dir = str(book)
    # --reanalyze forces ingest, which fails because the structure file is absent.
    with pytest.raises(Exception):
        Pipeline(cfg).run(
            structure_path="missing/audiobook_structure_0000-5min.json",
            reanalyze=True, dry_run=True,
        )
