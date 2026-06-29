"""Tests for transcription progress reporting (no real audio)."""

from audiobook_visualizer.config import Config
from audiobook_visualizer.pipeline import Pipeline


def test_transcription_progress_throttles_per_decile():
    messages: list[str] = []
    pipeline = Pipeline(Config(), on_progress=messages.append)
    report = pipeline._transcription_progress()

    # Feed many fine-grained updates over a 100s "file".
    for done in range(0, 101):
        report(float(done), 100.0)

    # Throttled to at most one line per decile (0%..100% -> 11 buckets).
    assert 0 < len(messages) <= 11
    assert any("100%" in m for m in messages)
    # Messages are monotonic in reported percentage.
    assert messages == sorted(messages, key=lambda m: int(m.split("%")[0].split()[-1]))


def test_transcription_progress_ignores_unknown_total():
    messages: list[str] = []
    pipeline = Pipeline(Config(), on_progress=messages.append)
    report = pipeline._transcription_progress()
    report(5.0, 0.0)  # total unknown -> no output
    assert messages == []


def test_on_progress_flows_through_transcribe_backend(monkeypatch):
    """transcribe_audio should forward on_progress to the chosen backend."""
    import audiobook_visualizer.ingest.audio as audio_mod
    from audiobook_visualizer.models import Transcript, TranscriptSegment

    seen: list[tuple[float, float]] = []

    def fake_local(path, model, on_progress, w_start, w_end, chunk_seconds):
        if on_progress:
            on_progress(10.0, 20.0)
            on_progress(20.0, 20.0)
        return Transcript(segments=[TranscriptSegment(start=0.0, end=20.0, text="hi")])

    monkeypatch.setattr(audio_mod, "_transcribe_local", fake_local)
    monkeypatch.setattr(audio_mod.Path, "exists", lambda self: True)

    audio_mod.transcribe_audio(
        "book.m4b", backend="faster-whisper", on_progress=lambda d, t: seen.append((d, t))
    )
    assert seen == [(10.0, 20.0), (20.0, 20.0)]
