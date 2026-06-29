"""Tests for audiobook chapter/paragraph segmentation (no audio/API needed)."""

from audiobook_visualizer.analysis.align import align_scenes_to_structure
from audiobook_visualizer.config import AudioConfig
from audiobook_visualizer.ingest.segmentation import (
    _segment_into_paragraphs,
    build_audiobook_structure,
)
from audiobook_visualizer.models import Scene, Transcript, TranscriptSegment


def _seg(start, end, text):
    return TranscriptSegment(start=start, end=end, text=text)


def test_paragraphs_split_on_sentence_ending_pause():
    cfg = AudioConfig()  # paragraph_gap=0.65
    segments = [
        _seg(0.0, 2.0, "He opened the door slowly."),
        _seg(2.1, 4.0, "The room was dark and cold."),  # small gap -> same para
        _seg(5.0, 7.0, "Years later, she returned."),  # 1.0s gap + sentence end -> split
        _seg(7.1, 9.0, "Nothing had changed."),
    ]
    paras = _segment_into_paragraphs(segments, cfg)
    assert len(paras) == 2
    assert paras[0].start == 0.0
    assert paras[0].end == 4.0
    assert paras[1].text.startswith("Years later")
    assert paras[1].start == 5.0


def test_paragraph_does_not_split_mid_sentence():
    cfg = AudioConfig()
    segments = [
        _seg(0.0, 2.0, "He walked for a long time and"),  # no sentence end
        _seg(3.0, 5.0, "eventually reached the sea."),  # gap but prev didn't end sentence
    ]
    paras = _segment_into_paragraphs(segments, cfg)
    assert len(paras) == 1


def test_chapters_from_spoken_headings():
    cfg = AudioConfig(chapter_mode="headings")
    transcript = Transcript(
        segments=[
            _seg(0.0, 2.0, "Chapter One."),
            _seg(2.1, 4.0, "It was a bright morning."),
            _seg(4.5, 6.0, "Chapter Two."),
            _seg(6.1, 8.0, "The storm arrived at dusk."),
        ]
    )
    structure = build_audiobook_structure(transcript, "nonexistent.m4b", cfg)
    assert structure.source == "headings"
    assert len(structure.chapters) == 2
    assert structure.chapters[0].title == "Chapter One"
    assert structure.chapters[1].title == "Chapter Two"


def test_time_based_segmentation():
    cfg = AudioConfig(chapter_mode="time", chapter_seconds=60)
    segments = [_seg(float(i), float(i) + 0.5, f"Sentence {i}.") for i in range(0, 150, 10)]
    transcript = Transcript(segments=segments)
    structure = build_audiobook_structure(transcript, "x.mp3", cfg)
    assert structure.source == "time"
    # 150s of audio / 60s windows -> 3 chapters.
    assert len(structure.chapters) == 3
    assert structure.chapters[0].title.startswith("Part 1")


def test_single_fallback_when_no_structure():
    cfg = AudioConfig(chapter_mode="headings")  # but no headings present
    transcript = Transcript(
        segments=[_seg(0.0, 2.0, "Just some narration."), _seg(2.1, 4.0, "And more.")]
    )
    structure = build_audiobook_structure(transcript, "x.mp3", cfg, title="My Book")
    assert structure.source == "single"
    assert len(structure.chapters) == 1
    assert structure.chapters[0].title == "My Book"


def test_align_scenes_to_structure_uses_paragraph_times():
    cfg = AudioConfig(chapter_mode="headings")
    transcript = Transcript(
        segments=[
            _seg(0.0, 2.0, "Chapter One."),
            _seg(2.1, 4.0, "The harbor lay grey under a cold dawn."),
            _seg(30.0, 32.0, "Chapter Two."),
            _seg(32.1, 34.0, "Ahab strode the quarterdeck at dusk."),
        ]
    )
    structure = build_audiobook_structure(transcript, "x.mp3", cfg)

    scenes = [
        Scene(id="s1", chapter="Chapter Two", source_excerpt="Ahab strode the quarterdeck"),
        Scene(id="s2", chapter="Chapter One", source_excerpt="The harbor lay grey under a cold dawn"),
    ]
    align_scenes_to_structure(scenes, structure)

    # Exact paragraph timestamps, not a fuzzy guess.
    assert scenes[0].start_time == 32.1
    assert scenes[0].end_time == 34.0
    assert scenes[1].start_time == 2.1


def test_align_scenes_to_structure_ignores_short_excerpt():
    cfg = AudioConfig(chapter_mode="single")
    transcript = Transcript(segments=[_seg(0.0, 2.0, "Some narration here that is long enough.")])
    structure = build_audiobook_structure(transcript, "x.mp3", cfg)
    scene = Scene(id="s1", source_excerpt="too short")  # < 12 normalized chars
    align_scenes_to_structure([scene], structure)
    assert scene.start_time is None


def test_as_chapters_shape_feeds_analyzer():
    cfg = AudioConfig(chapter_mode="headings")
    transcript = Transcript(
        segments=[
            _seg(0.0, 2.0, "Chapter One."),
            _seg(2.1, 4.0, "A beginning."),
            _seg(5.0, 6.0, "Chapter Two."),
            _seg(6.1, 8.0, "An ending."),
        ]
    )
    structure = build_audiobook_structure(transcript, "x.mp3", cfg)
    chapters = structure.as_chapters
    assert all(isinstance(c, tuple) and len(c) == 2 for c in chapters)
    assert chapters[0][0] == "Chapter One"
    assert "beginning" in chapters[0][1].lower()
