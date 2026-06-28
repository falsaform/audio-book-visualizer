"""Tests for caching, character-portrait references, shot-type, and Gemini."""

import sys
import types
from pathlib import Path

import pytest

from audiobook_visualizer.config import Config
from audiobook_visualizer.generation import build_prompt
from audiobook_visualizer.generation.cache import compute_key, is_cached, write_sidecar
from audiobook_visualizer.models import BookAnalysis, Character, Scene
from audiobook_visualizer.pipeline import Pipeline, _scene_references


def _analysis() -> BookAnalysis:
    return BookAnalysis(
        title="Test",
        characters=[
            Character(name="Ahab", aliases=["Captain Ahab"], description="scarred captain"),
            Character(name="Ishmael", description="lean young sailor"),
        ],
        scenes=[
            Scene(
                id="scene_001_00",
                title="On deck",
                visual_description="A captain at the helm.",
                shot_type="wide establishing shot",
                characters_present=["Captain Ahab"],
            ),
        ],
    )


# -- shot type --------------------------------------------------------------


def test_shot_type_included_and_toggleable():
    scene = _analysis().scenes[0]
    with_shot = build_prompt(scene, _analysis(), "noir", include_shot=True)
    without = build_prompt(scene, _analysis(), "noir", include_shot=False)
    assert "wide establishing shot" in with_shot
    assert "wide establishing shot" not in without


# -- caching ----------------------------------------------------------------


def test_cache_sidecar_roundtrip(tmp_path: Path):
    img = tmp_path / "frame.png"
    img.write_bytes(b"img")
    key = compute_key({"prompt": "a", "provider": "stub"})
    assert not is_cached(img, key)  # no sidecar yet
    write_sidecar(img, key)
    assert is_cached(img, key)
    assert not is_cached(img, compute_key({"prompt": "different"}))


def test_second_run_uses_cache(tmp_path: Path):
    cfg = Config()
    cfg.output.dir = str(tmp_path)
    pipeline = Pipeline(cfg)

    first = pipeline.generate(_analysis(), tmp_path, dry_run=True)
    assert all(not f.cached for f in first)
    assert all(f.ok for f in first)

    second = pipeline.generate(_analysis(), tmp_path, dry_run=True)
    assert all(f.cached for f in second)

    forced = pipeline.generate(_analysis(), tmp_path, dry_run=True, force=True)
    assert all(not f.cached for f in forced)


# -- portraits / references -------------------------------------------------


def test_portraits_generated_and_referenced(tmp_path: Path):
    cfg = Config()
    cfg.output.dir = str(tmp_path)
    pipeline = Pipeline(cfg)

    frames = pipeline.generate(_analysis(), tmp_path, dry_run=True)

    # The stub provider supports references, so a portrait is rendered for the
    # character that appears (Ahab, referenced by his alias) — but not Ishmael,
    # who is in no rendered scene.
    portraits = list((tmp_path / "portraits").glob("*.png"))
    names = {p.stem for p in portraits}
    assert "ahab" in names
    assert "ishmael" not in names
    # The scene's frame records the portrait as a reference.
    assert frames[0].references and "ahab" in frames[0].references[0]


def test_scene_references_resolves_alias_to_canonical_portrait(tmp_path: Path):
    analysis = _analysis()
    portrait = tmp_path / "ahab.png"
    portrait.write_bytes(b"x")
    portraits = {"ahab": portrait}  # keyed by canonical lowercased name
    refs = _scene_references(analysis.scenes[0], analysis, portraits)
    assert refs == [portrait]  # alias "Captain Ahab" resolved to Ahab's portrait


# -- gemini provider --------------------------------------------------------


def test_gemini_provider_writes_image(monkeypatch, tmp_path: Path):
    # Fake the google-genai SDK and PIL so no network/keys are needed.
    image_bytes = b"\x89PNG\r\n\x1a\n-fake-gemini-image"

    class FakeInline:
        data = image_bytes

    class FakePart:
        inline_data = FakeInline()

    class FakeContent:
        parts = [FakePart()]

    class FakeCandidate:
        content = FakeContent()

    class FakeResp:
        candidates = [FakeCandidate()]

    class FakeModels:
        def generate_content(self, model, contents):
            FakeModels.captured = {"model": model, "contents": contents}
            return FakeResp()

    class FakeClient:
        def __init__(self, api_key=None):
            self.models = FakeModels()

    fake_genai = types.SimpleNamespace(Client=FakeClient)
    monkeypatch.setitem(sys.modules, "google", types.SimpleNamespace(genai=fake_genai))
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    from audiobook_visualizer.generation.gemini_provider import GeminiImageProvider

    provider = GeminiImageProvider(model="gemini-2.5-flash-image")
    assert provider.supports_references is True
    out = provider.generate("a stormy sea", tmp_path / "frame")
    assert out.exists()
    assert out.read_bytes() == image_bytes


def test_resolve_provider_auto_picks_from_keys(monkeypatch):
    from audiobook_visualizer.generation.factory import _resolve_provider

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    # Only Gemini key -> gemini.
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    assert _resolve_provider("auto", "dall-e-3") == "gemini"

    # Both keys -> prefer openai (the documented default).
    monkeypatch.setenv("OPENAI_API_KEY", "o-key")
    assert _resolve_provider("auto", "dall-e-3") == "openai"

    # Placeholder OPENAI key is ignored -> falls through to gemini.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-...")
    assert _resolve_provider("auto", "dall-e-3") == "gemini"

    # Explicit provider is respected.
    assert _resolve_provider("openai", "dall-e-3") == "openai"


def test_resolve_provider_auto_no_keys_errors(monkeypatch):
    from audiobook_visualizer.generation.factory import _resolve_provider

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="No image-generation credentials"):
        _resolve_provider("auto", "dall-e-3")


def test_gemini_error_classification():
    from audiobook_visualizer.generation import gemini_provider as gp

    hard = "429 RESOURCE_EXHAUSTED ... limit: 0, model: gemini-2.5-flash-image"
    soft = "429 RESOURCE_EXHAUSTED PerMinute ... 'Please retry in 11.5s'"
    assert gp._is_quota_error(Exception(hard))
    assert gp._is_quota_error(Exception(soft))
    assert gp._is_hard_quota(Exception(hard))  # limit: 0 -> hard
    assert not gp._is_hard_quota(Exception(soft))
    assert not gp._is_quota_error(Exception("500 internal error"))
    # retry-after parsed from the payload.
    assert gp._retry_after(Exception("Please retry in 11.5s"), 99) == 11.5
    assert gp._retry_after(Exception("no hint"), 7.0) == 7.0


def test_gemini_human_message_strips_json():
    from audiobook_visualizer.generation import gemini_provider as gp

    err = "ClientError ... {'error': {'message': 'You exceeded your current quota', 'code': 429}}"
    msg = gp._quota_message(Exception(err), hard=True)
    assert "You exceeded your current quota" in msg
    assert "billing" in msg
    assert "{" not in msg  # no raw JSON in the surfaced message


def test_factory_selects_gemini(monkeypatch):
    captured = {}

    class FakeGemini:
        def __init__(self, model=""):
            captured["model"] = model

    import audiobook_visualizer.generation.factory as factory
    monkeypatch.setattr(
        "audiobook_visualizer.generation.gemini_provider.GeminiImageProvider", FakeGemini
    )
    cfg = Config().generation
    cfg.provider = "gemini"
    cfg.model = "dall-e-3"  # non-gemini model -> factory should blank it
    factory.get_provider(cfg)
    assert captured["model"] == ""
