"""Unit tests that run without any API access."""

from pathlib import Path

from audiobook_visualizer.analysis.align import align_scenes
from audiobook_visualizer.config import Config
from audiobook_visualizer.generation.prompt_builder import build_prompt
from audiobook_visualizer.generation.stub_provider import StubImageProvider
from audiobook_visualizer.ingest.ebook import load_ebook
from audiobook_visualizer.models import (
    BookAnalysis,
    Character,
    Scene,
    Transcript,
    TranscriptSegment,
)
from audiobook_visualizer.utils.llm import _parse_json

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "sample.txt"


def test_config_defaults():
    cfg = Config()
    assert cfg.analysis.provider == "anthropic"
    assert cfg.generation.provider == "openai"
    assert "16:9" in cfg.project.style


def test_load_ebook_splits_chapters():
    book = load_ebook(EXAMPLE)
    titles = [c.title.lower() for c in book.chapters]
    assert any("chapter 1" in t for t in titles)
    assert any("chapter 2" in t for t in titles)
    assert "Ishmael" in book.full_text


def test_parse_json_handles_fences():
    assert _parse_json('```json\n[{"a": 1}]\n```') == [{"a": 1}]
    assert _parse_json('here you go: {"x": 2} done') == {"x": 2}


def test_character_matching_by_alias():
    char = Character(name="Ahab", aliases=["Captain Ahab"], description="scarred")
    assert char.matches("captain ahab")
    assert char.matches("Ahab")
    assert not char.matches("Ishmael")


def test_build_prompt_injects_character_appearance():
    analysis = BookAnalysis(
        characters=[Character(name="Ahab", description="a tall grizzled man with a white scar")]
    )
    scene = Scene(
        id="s1",
        visual_description="A captain stands at the helm.",
        characters_present=["Ahab"],
        setting="ship deck",
        time_of_day="dawn",
        mood="ominous",
    )
    prompt = build_prompt(scene, analysis, style="oil painting")
    assert "white scar" in prompt
    assert "ship deck" in prompt
    assert "oil painting" in prompt
    assert "No text" in prompt


def test_stub_provider_writes_file(tmp_path):
    provider = StubImageProvider(size="320x180")
    out = provider.generate("a stormy sea", tmp_path / "frame")
    assert out.exists()
    assert out.suffix == ".png"
    assert out.stat().st_size > 0


def test_align_scenes_attaches_timestamps():
    scenes = [Scene(id="s1", source_excerpt="the green and heaving sea")]
    transcript = Transcript(
        segments=[
            TranscriptSegment(start=0.0, end=5.0, text="Call me Ishmael."),
            TranscriptSegment(start=5.0, end=10.0, text="upon a green and heaving sea"),
        ]
    )
    align_scenes(scenes, transcript)
    assert scenes[0].start_time == 5.0
