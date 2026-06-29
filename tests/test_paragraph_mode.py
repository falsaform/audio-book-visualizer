"""Per-paragraph analysis mode (one frame per paragraph, narration-timed)."""

from audiobook_visualizer.analysis.analyzer import Analyzer
from audiobook_visualizer.config import AnalysisConfig
from audiobook_visualizer.models import AudioChapter, AudiobookStructure, Paragraph


class FakeClient:
    """Returns one description per passage; records calls."""

    def __init__(self):
        self.calls = 0

    def complete_json(self, system, prompt, max_tokens=4096):
        self.calls += 1
        # Count the numbered passages in the prompt and echo descriptions.
        import re

        n = len(re.findall(r"^\[\d+\]", prompt, re.MULTILINE))
        if "storyboard artist creating one still frame per" in system.lower() \
                or "ONE still frame" in prompt:
            return [
                {"visual_description": f"frame {i}", "characters_present": ["Ahab"],
                 "shot_type": "medium shot"}
                for i in range(n)
            ]
        # Character bible call -> a small list.
        return [{"name": "Ahab", "description": "scarred captain"}]


def _structure() -> AudiobookStructure:
    return AudiobookStructure(
        title="Moby",
        chapters=[
            AudioChapter(
                index=0, title="Chapter One", start=0.0, end=40.0,
                paragraphs=[
                    Paragraph(index=0, text="Call me Ishmael.", start=0.0, end=10.0),
                    Paragraph(index=1, text="The harbor lay grey.", start=10.0, end=20.0),
                    Paragraph(index=2, text="Ahab paced the deck.", start=20.0, end=40.0),
                ],
            )
        ],
    )


def _analyzer() -> Analyzer:
    a = Analyzer.__new__(Analyzer)
    a.config = AnalysisConfig(mode="paragraphs", paragraphs_per_scene=1)
    a._client = FakeClient()
    a._progress = lambda _m: None
    return a


def test_one_scene_per_paragraph_with_exact_timestamps():
    analyzer = _analyzer()
    analysis = analyzer.analyze_paragraphs(_structure(), title="Moby")

    assert len(analysis.scenes) == 3  # one per paragraph
    # Timestamps come straight from the paragraphs (no alignment).
    assert [(s.start_time, s.end_time) for s in analysis.scenes] == [
        (0.0, 10.0), (10.0, 20.0), (20.0, 40.0)
    ]
    assert all(s.visual_description for s in analysis.scenes)
    assert all(s.chapter == "Chapter One" for s in analysis.scenes)
    # Scene ids are unique and ordered.
    assert len({s.id for s in analysis.scenes}) == 3


def test_paragraphs_per_scene_groups():
    analyzer = _analyzer()
    analyzer.config.paragraphs_per_scene = 2
    analysis = analyzer.analyze_paragraphs(_structure())

    # 3 paragraphs grouped by 2 -> 2 frames; first spans paras 0-1.
    assert len(analysis.scenes) == 2
    assert analysis.scenes[0].start_time == 0.0 and analysis.scenes[0].end_time == 20.0
    assert analysis.scenes[1].start_time == 20.0 and analysis.scenes[1].end_time == 40.0


def test_units_have_continuous_unique_ids():
    analyzer = _analyzer()
    units = analyzer._paragraph_units(_structure(), id_prefix="0000-5min")
    ids = [u["id"] for u in units]
    assert ids == ["0000-5min_001_0000", "0000-5min_001_0001", "0000-5min_001_0002"]
