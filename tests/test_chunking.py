"""Tests for memory-bounded chunked transcription (ffmpeg/whisper stubbed)."""

import sys
import types
from pathlib import Path

import audiobook_visualizer.ingest.audio as audio_mod


def test_iter_chunks_offsets_and_cleanup(monkeypatch, tmp_path):
    extracted: list[tuple[float, float]] = []

    def fake_extract(path, start, length, out):
        extracted.append((start, length))
        out.write_bytes(b"RIFFfake")

    monkeypatch.setattr(audio_mod, "_extract_chunk", fake_extract)

    parts = []
    for part, offset in audio_mod._iter_chunks(Path("in.m4b"), 25.0, 10.0, tmp_path):
        parts.append((part.name, offset))
        assert part.exists()  # file is present while being handled

    offsets = [off for _, off in parts]
    assert offsets == [0.0, 10.0, 20.0]
    # Last chunk is clamped to the remaining 5s (25 - 20).
    assert extracted[-1] == (20.0, 5.0)
    # Every chunk file is deleted after it is yielded.
    assert list(tmp_path.glob("chunk_*.wav")) == []


def test_transcribe_local_stitches_offsets(monkeypatch):
    # Fake faster_whisper: every chunk yields two 1s segments.
    class FakeSeg:
        def __init__(self, s, e, t):
            self.start, self.end, self.text = s, e, t

    class FakeInfo:
        duration = 10.0
        language = "en"

    class FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, path, **k):
            return iter([FakeSeg(0.0, 1.0, "a"), FakeSeg(1.0, 2.0, "b")]), FakeInfo()

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeModel))
    monkeypatch.setattr(audio_mod, "_probe_duration", lambda p: 20.0)
    monkeypatch.setattr(audio_mod.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(audio_mod.Path, "exists", lambda self: True)
    monkeypatch.setattr(audio_mod, "_extract_chunk", lambda p, s, length, out: out.write_bytes(b"x"))

    progress: list[tuple[float, float]] = []
    tr = audio_mod.transcribe_audio(
        "book.m4b",
        backend="faster-whisper",
        chunk_seconds=10,
        on_progress=lambda d, t: progress.append((d, t)),
    )

    # Two chunks (20s / 10s) at offsets 0 and 10, each contributing 2 segments.
    assert [s.start for s in tr.segments] == [0.0, 1.0, 10.0, 11.0]
    assert [s.text for s in tr.segments] == ["a", "b", "a", "b"]
    assert tr.language == "en"
    # Progress is reported against the global total and finishes at 100%.
    assert progress[-1] == (20.0, 20.0)
    assert all(t == 20.0 for _, t in progress)


def test_no_chunk_when_ffmpeg_missing(monkeypatch):
    """Without ffmpeg we fall back to a single whole-file pass (offset 0)."""
    seen: list[tuple[str, float]] = []

    class FakeInfo:
        duration = 30.0
        language = None

    class FakeSeg:
        start, end, text = 0.0, 3.0, "whole"

    class FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, path, **k):
            seen.append((path, 0.0))
            return iter([FakeSeg()]), FakeInfo()

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeModel))
    monkeypatch.setattr(audio_mod, "_probe_duration", lambda p: 30.0)
    monkeypatch.setattr(audio_mod.shutil, "which", lambda name: None)  # no ffmpeg
    monkeypatch.setattr(audio_mod.Path, "exists", lambda self: True)

    def boom(*a, **k):
        raise AssertionError("must not chunk without ffmpeg")

    monkeypatch.setattr(audio_mod, "_extract_chunk", boom)

    tr = audio_mod.transcribe_audio("book.m4b", backend="faster-whisper", chunk_seconds=10)
    assert len(tr.segments) == 1
    assert seen and seen[0][1] == 0.0
