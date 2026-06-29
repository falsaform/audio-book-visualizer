"""Reusing an existing audiobook_structure.json in `visualize` (no re-transcribe)."""

from pathlib import Path

import audiobook_visualizer.pipeline as pipeline_mod
from audiobook_visualizer.config import Config
from audiobook_visualizer.models import AudioChapter, AudiobookStructure, Paragraph
from audiobook_visualizer.pipeline import Pipeline


def _structure_file(tmp_path: Path) -> Path:
    structure = AudiobookStructure(
        title="Moby Dick",
        source="headings",
        chapters=[
            AudioChapter(
                index=0, title="Chapter One", start=0.0, end=8.0,
                paragraphs=[Paragraph(index=0, text="Call me Ishmael.", start=0.0, end=8.0)],
            ),
            AudioChapter(
                index=1, title="Chapter Two", start=30.0, end=40.0,
                paragraphs=[Paragraph(index=0, text="The Pequod set sail.", start=30.0, end=40.0)],
            ),
        ],
    )
    path = tmp_path / "audiobook_structure.json"
    path.write_text(structure.model_dump_json(indent=2))
    return path


def test_ingest_loads_structure_without_transcribing(tmp_path, monkeypatch):
    # Fail loudly if transcription is attempted.
    def boom(*a, **k):
        raise AssertionError("transcribe_audio must not be called when reusing structure")

    monkeypatch.setattr(pipeline_mod, "transcribe_audio", boom)

    path = _structure_file(tmp_path)
    result = Pipeline(Config()).ingest(None, structure_path=str(path))

    assert result.transcript is None
    assert result.structure is not None
    assert result.title == "Moby Dick"
    assert [t for t, _ in result.chapters] == ["Chapter One", "Chapter Two"]
    assert "Ishmael" in result.chapters[0][1]


def test_run_with_structure_reaches_analysis(tmp_path, monkeypatch):
    # Stub analysis so no API is needed; assert it receives the loaded structure.
    captured = {}

    def fake_analyze(
        self, chapters, title, author, structure=None,
        id_prefix="scene", known_characters=None,
    ):
        captured["chapters"] = chapters
        captured["structure"] = structure
        captured["id_prefix"] = id_prefix
        from audiobook_visualizer.models import BookAnalysis
        return BookAnalysis(title=title, scenes=[])

    monkeypatch.setattr(Pipeline, "analyze", fake_analyze)
    monkeypatch.setattr(pipeline_mod, "transcribe_audio",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no transcribe")))

    path = _structure_file(tmp_path)
    cfg = Config()
    cfg.output.dir = str(tmp_path)
    analysis = Pipeline(cfg).run(structure_path=str(path), analyze_only=True)

    assert analysis.title == "Moby Dick"
    assert captured["structure"] is not None
    assert len(captured["chapters"]) == 2
    # The structure is re-persisted in the output dir.
    assert (tmp_path / "audiobook_structure.json").exists()
