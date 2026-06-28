"""Unit tests that run without any API access."""

from audiobook_visualizer.config import Config
from audiobook_visualizer.generation.prompt_builder import build_prompt
from audiobook_visualizer.generation.stub_provider import StubImageProvider
from audiobook_visualizer.models import BookAnalysis, Character, Scene
from audiobook_visualizer.utils.llm import _parse_json


def test_config_defaults():
    cfg = Config()
    assert cfg.analysis.provider == "auto"
    assert cfg.generation.provider == "auto"
    assert "16:9" in cfg.project.style


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
