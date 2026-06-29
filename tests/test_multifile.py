"""Tests for multi-file (folder) audiobook input."""

import sys
import types
from pathlib import Path

import audiobook_visualizer.ingest.audio as audio_mod
from audiobook_visualizer.config import AudioConfig
from audiobook_visualizer.ingest import build_audiobook_structure
from audiobook_visualizer.models import Transcript, TranscriptSegment


def _touch(d: Path, name: str) -> None:
    (d / name).write_bytes(b"x")


def test_gather_audio_files_natural_sort_and_filter(tmp_path):
    for name in ["Part 10.mp3", "Part 2.mp3", "Part 1.mp3", "cover.jpg", "notes.txt"]:
        _touch(tmp_path, name)
    files = audio_mod.gather_audio_files(tmp_path)
    assert [f.name for f in files] == ["Part 1.mp3", "Part 2.mp3", "Part 10.mp3"]


def test_gather_single_file(tmp_path):
    f = tmp_path / "book.m4b"
    f.write_bytes(b"x")
    assert audio_mod.gather_audio_files(f) == [f]


def test_audio_file_boundaries(tmp_path, monkeypatch):
    for name in ["01.mp3", "02.mp3", "03.mp3"]:
        _touch(tmp_path, name)
    monkeypatch.setattr(audio_mod, "_probe_duration", lambda p: 60.0)
    bounds = audio_mod.audio_file_boundaries(tmp_path)
    assert bounds == [("01", 0.0, 60.0), ("02", 60.0, 120.0), ("03", 120.0, 180.0)]


def test_audio_file_boundaries_single_file_is_none(tmp_path, monkeypatch):
    f = tmp_path / "book.m4b"
    f.write_bytes(b"x")
    monkeypatch.setattr(audio_mod, "_probe_duration", lambda p: 60.0)
    assert audio_mod.audio_file_boundaries(f) is None


def test_transcribe_folder_continuous_timestamps(tmp_path, monkeypatch):
    for name in ["01.mp3", "02.mp3"]:
        _touch(tmp_path, name)

    class FakeSeg:
        def __init__(self, s, e, t):
            self.start, self.end, self.text = s, e, t

    class FakeInfo:
        duration = 60.0
        language = "en"

    class FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, path, **k):
            return iter([FakeSeg(0.0, 5.0, "hi")]), FakeInfo()

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeModel))
    monkeypatch.setattr(audio_mod, "_probe_duration", lambda p: 60.0)
    monkeypatch.setattr(audio_mod.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(audio_mod, "_extract_chunk", lambda p, s, length, out: out.write_bytes(b"x"))

    tr = audio_mod.transcribe_audio(tmp_path, backend="faster-whisper", chunk_seconds=600)
    # File 1 segment at 0; file 2's segment is offset by file 1's 60s duration.
    assert [s.start for s in tr.segments] == [0.0, 60.0]
    assert [s.end for s in tr.segments] == [5.0, 65.0]


def test_structure_from_file_boundaries(tmp_path):
    transcript = Transcript(
        segments=[
            TranscriptSegment(start=10.0, end=12.0, text="Inside part one."),
            TranscriptSegment(start=70.0, end=72.0, text="Inside part two."),
        ]
    )
    boundaries = [("01 - The Loomings", 0.0, 60.0), ("02 - The Carpet-Bag", 60.0, 120.0)]
    structure = build_audiobook_structure(
        transcript, str(tmp_path), AudioConfig(), title="Moby", file_boundaries=boundaries
    )
    assert structure.source == "files"
    assert [c.title for c in structure.chapters] == ["01 - The Loomings", "02 - The Carpet-Bag"]
    assert "part one" in structure.chapters[0].text.lower()
    assert "part two" in structure.chapters[1].text.lower()
