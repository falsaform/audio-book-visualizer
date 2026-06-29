"""Tests for per-book output subfolders and joining sub-segment structures."""

from pathlib import Path

from audiobook_visualizer.cli import _resolve_output_dir
from audiobook_visualizer.config import Config
from audiobook_visualizer.ingest import join_structures
from audiobook_visualizer.models import AudioChapter, AudiobookStructure, Paragraph
from audiobook_visualizer.utils import slugify


def test_slugify():
    assert slugify("Moby Dick.m4b") == "moby-dick-m4b"
    assert slugify("  The Hobbit!  ") == "the-hobbit"
    assert slugify("???") == "book"


def test_output_dir_per_book_subfolder():
    cfg = Config()
    cfg.output.dir = "output"
    # No --out -> subfolder from the audio stem.
    assert _resolve_output_dir(cfg, None, audio=Path("/x/Moby Dick.m4b")) == "output/moby-dick"
    assert _resolve_output_dir(cfg, None, audio=Path("hobbit.mp3")) == "output/hobbit"


def test_output_dir_explicit_out_wins():
    cfg = Config()
    cfg.output.dir = "output"
    assert _resolve_output_dir(cfg, Path("custom/dir"), audio=Path("x.m4b")) == "custom/dir"


def test_output_dir_structure_uses_its_parent():
    cfg = Config()
    cfg.output.dir = "output"
    got = _resolve_output_dir(cfg, None, structure=Path("output/moby/audiobook_structure.json"))
    assert got == "output/moby"


def _seg(start, title, paras):
    return AudioChapter(
        index=0, title=title, start=start, end=start + 10,
        paragraphs=[Paragraph(index=i, text=t, start=start, end=start + 10) for i, t in enumerate(paras)],
    )


def test_join_orders_by_time_and_reindexes():
    a = AudiobookStructure(title="Book", chapters=[_seg(60.0, "Chapter Two", ["b"])])
    b = AudiobookStructure(title="Book", chapters=[_seg(0.0, "Chapter One", ["a"])])
    joined = join_structures([a, b])  # out of order on purpose

    assert joined.source == "joined"
    assert [c.title for c in joined.chapters] == ["Chapter One", "Chapter Two"]
    assert [c.index for c in joined.chapters] == [0, 1]


def test_join_stitches_same_title_chapter_split_across_windows():
    # "Chapter Five" split at a window boundary -> one merged chapter.
    first = AudiobookStructure(chapters=[_seg(50.0, "Chapter Five", ["first half"])])
    second = AudiobookStructure(chapters=[_seg(60.0, "Chapter Five", ["second half"])])
    joined = join_structures([first, second])

    assert len(joined.chapters) == 1
    ch = joined.chapters[0]
    assert [p.text for p in ch.paragraphs] == ["first half", "second half"]
    assert [p.index for p in ch.paragraphs] == [0, 1]  # paragraphs re-indexed
    assert ch.end == 70.0  # extended to the later window's end
