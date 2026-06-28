"""Per-chunk rendering: no overwrite, shared+cached portraits, character bible."""

from audiobook_visualizer.config import Config
from audiobook_visualizer.models import BookAnalysis, Character, Scene
from audiobook_visualizer.pipeline import (
    Pipeline,
    _chunk_label,
    _load_characters,
    _save_characters,
)


def test_chunk_label_from_structure_filename():
    assert _chunk_label("output/legion/audiobook_structure_0000-5min.json") == "0000-5min"
    assert _chunk_label("x/audiobook_structure_0060-120min.json") == "0060-120min"
    assert _chunk_label("x/audiobook_structure.json") == "full"


def test_character_bible_roundtrip(tmp_path):
    chars = [Character(name="Ahab", aliases=["Captain"], description="scarred")]
    _save_characters(tmp_path, chars)
    loaded = _load_characters(tmp_path)
    assert [c.name for c in loaded] == ["Ahab"]
    assert loaded[0].aliases == ["Captain"]
    # Missing file -> empty.
    assert _load_characters(tmp_path / "nope") == []


def _analysis(scene_id: str) -> BookAnalysis:
    return BookAnalysis(
        title="Legion",
        characters=[Character(name="Bob", description="tall man in a grey coat")],
        scenes=[Scene(id=scene_id, title="x", visual_description="a man", characters_present=["Bob"])],
    )


def test_portraits_shared_and_cached_across_chunks(tmp_path):
    book = tmp_path / "legion"
    cfg = Config()
    pipeline = Pipeline(cfg)

    # Chunk A and chunk B render into separate dirs but share book-level portraits.
    chunk_a, chunk_b = book / "0000-5min", book / "0005-20min"
    pipeline.generate(_analysis("0000-5min_001_00"), chunk_a, book_dir=book, dry_run=True)
    fb = pipeline.generate(_analysis("0005-20min_001_00"), chunk_b, book_dir=book, dry_run=True)

    # Frames live in their own chunk folders (no overwrite).
    assert (chunk_a / "frames" / "0000-5min_001_00.png").exists()
    assert (chunk_b / "frames" / "0005-20min_001_00.png").exists()

    # Exactly one shared portrait for Bob (same appearance -> reused, not duplicated).
    portraits = list((book / "portraits").glob("*.png"))
    assert len(portraits) == 1
    assert portraits[0].stem.startswith("bob_")

    # Second chunk reused the cached portrait.
    assert fb[0].ok


def test_changed_appearance_makes_new_portrait(tmp_path):
    book = tmp_path / "book"
    pipeline = Pipeline(Config())

    young = BookAnalysis(
        characters=[Character(name="Bob", description="a young man")],
        scenes=[Scene(id="a_001_00", visual_description="x", characters_present=["Bob"])],
    )
    old = BookAnalysis(
        characters=[Character(name="Bob", description="an old grey-bearded man")],
        scenes=[Scene(id="b_001_00", visual_description="x", characters_present=["Bob"])],
    )
    pipeline.generate(young, book / "a", book_dir=book, dry_run=True)
    pipeline.generate(old, book / "b", book_dir=book, dry_run=True)

    # Appearance changed -> two distinct portraits kept (evolution over the book).
    portraits = sorted(p.stem for p in (book / "portraits").glob("bob_*.png"))
    assert len(portraits) == 2
