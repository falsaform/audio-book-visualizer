"""End-to-end director mode through the pipeline (LLM seam mocked, stub images)."""

from pathlib import Path

import audiobook_visualizer.analysis.analyzer as anmod
import audiobook_visualizer.crew.agent as agmod
from audiobook_visualizer.config import Config
from audiobook_visualizer.models import AudioChapter, AudiobookStructure, Paragraph
from audiobook_visualizer.pipeline import Pipeline
from audiobook_visualizer.store import ProductionStore


class FakeClient:
    """One client standing in for character-bible, screenwriter and director calls."""

    model = "fake"

    def complete_json(self, system, prompt, max_tokens=4096):
        s = system.lower()
        # Check the specific roles before "screenwriter": the critic's system
        # prompt also contains that word.
        if "script editor" in s:
            return {"revise": False, "notes": []}  # critic accepts the breakdown
        if "continuity" in s:
            return [{"severity": "warning", "category": "wardrobe",
                     "message": "Ahab's coat changes color between shots",
                     "proposed_fix": "fix the coat"}]
        if "screenwriter" in s:
            return [{"start_paragraph": 0, "heading": "EXT. HARBOR - DAWN", "title": "Arrival",
                     "synopsis": "x", "action": "y", "setting": "harbor", "time_of_day": "dawn",
                     "mood": "cold", "characters_present": ["Ahab"]}]
        if "director" in s:
            return [
                {"shot_type": "wide", "camera_move": "push_in", "subject": "harbor",
                 "visual_description": "grey harbor", "characters_present": [], "duration_weight": 1},
                {"shot_type": "close-up", "camera_move": "tilt_up", "subject": "Ahab",
                 "visual_description": "the captain", "characters_present": ["Ahab"],
                 "duration_weight": 1},
            ]
        # character bible
        return [{"name": "Ahab", "aliases": [], "role": "captain", "description": "scarred captain"}]


def _structure_file(tmp_path: Path) -> Path:
    structure = AudiobookStructure(title="Moby", source="time", chapters=[
        AudioChapter(index=0, title="Chapter One", start=0.0, end=120.0, paragraphs=[
            Paragraph(index=0, text="Call me Ishmael.", start=0.0, end=60.0),
            Paragraph(index=1, text="Ahab paced the deck.", start=60.0, end=120.0),
        ]),
    ])
    path = tmp_path / "audiobook_structure_0000-2min.json"
    path.write_text(structure.model_dump_json())
    return path


def test_director_mode_writes_screenplay_and_shots(tmp_path, monkeypatch):
    monkeypatch.setattr(anmod, "build_llm_client", lambda cfg: FakeClient())
    monkeypatch.setattr(agmod, "build_llm_client", lambda cfg: FakeClient())

    book = tmp_path / "moby"
    cfg = Config()
    cfg.output.dir = str(book)
    cfg.analysis.mode = "director"
    cfg.crew.enabled = True

    Pipeline(cfg).run(structure_path=str(_structure_file(tmp_path)), dry_run=True)

    store = ProductionStore.open(book)
    pid = store.get_production_id()
    seg_id = store.find_segment(pid, "0000-2min")
    view = store.segment_view(seg_id)

    # A real (non-synthetic) screenplay scene broken into 2 shots with camera moves.
    assert len(view.shots) == 2
    assert {sh.camera_move for sh in view.shots} == {"push_in", "tilt_up"}
    assert all(sh.heading == "EXT. HARBOR - DAWN" for sh in view.shots)
    # Window comes from the chunk label (0-2min); shots tile it and the last hits 120.
    assert view.start == 0.0 and view.end == 120.0
    assert view.shots[0].start_time == 0.0 and view.shots[-1].end_time == 120.0
    # Frames were generated (stub) for each shot.
    frames = sorted(p.name for p in (book / "0000-2min" / "frames").glob("*.png"))
    assert len(frames) == 2
    # The continuity supervisor's note was persisted.
    notes = store.list_continuity_notes(seg_id)
    assert len(notes) == 1 and notes[0].category == "wardrobe"

    # The camera moves reach the video layer's cues (drives per-shot motion).
    from audiobook_visualizer.video import collect_segments
    segs = collect_segments(book)
    assert {c.move for c in segs[0].cues} == {"push_in", "tilt_up"}


def test_reanalyze_from_db_needs_no_structure_file(tmp_path, monkeypatch):
    monkeypatch.setattr(anmod, "build_llm_client", lambda cfg: FakeClient())
    monkeypatch.setattr(agmod, "build_llm_client", lambda cfg: FakeClient())

    import os

    book = tmp_path / "moby"
    cfg = Config()
    cfg.output.dir = str(book)
    cfg.analysis.mode = "director"
    cfg.crew.enabled = True

    # Plan-only run from the structure file (the "review before generate" entry).
    struct = _structure_file(tmp_path)  # audiobook_structure_0000-2min.json
    Pipeline(cfg).run(structure_path=str(struct), analyze_only=True)

    store = ProductionStore.open(book)
    pid = store.get_production_id()
    seg_id = store.find_segment(pid, "0000-2min")
    assert store.get_segment_structure(seg_id) is not None  # structure is in the DB

    # Delete the file and re-analyze the segment straight from the DB.
    os.remove(struct)
    analysis = Pipeline(cfg).run(segment_label="0000-2min", reanalyze=True, analyze_only=True)
    assert analysis is not None and len(analysis.scenes) == 2


def test_director_without_structure_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(anmod, "build_llm_client", lambda cfg: FakeClient())

    from audiobook_visualizer.pipeline import IngestResult

    # Pretend ingest produced chapters + a transcript but no structure (e.g. audio
    # with segmentation off): director mode can't run -> it falls back to scene mode.
    monkeypatch.setattr(
        Pipeline, "ingest",
        lambda self, audio_path, structure_path=None: IngestResult(
            chapters=[("Chapter One", "Call me Ishmael. Ahab paced the deck and brooded.")],
            title="Book", transcript=None, structure=None),
    )

    cfg = Config()
    cfg.output.dir = str(tmp_path / "book")
    cfg.analysis.mode = "director"
    cfg.crew.enabled = True

    analysis = Pipeline(cfg).run(audio_path="x.m4b", analyze_only=True)
    assert analysis is not None  # fell back to scene mode, no crash
