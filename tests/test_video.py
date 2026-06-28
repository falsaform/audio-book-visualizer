"""Tests for per-segment video compilation (ffmpeg mocked)."""

import json
from pathlib import Path

import pytest

import audiobook_visualizer.video as video_mod
from audiobook_visualizer.models import BookAnalysis, Frame, Scene
from audiobook_visualizer.video import (
    FrameCue,
    _frames_concat,
    _window,
    collect_segments,
    compile_videos,
)


def _chunk(book: Path, label: str, scenes_frames: list[tuple[str, float, float]]):
    """Create a chunk dir with analysis.json, manifest.json and frame PNGs."""
    d = book / label if label else book
    (d / "frames").mkdir(parents=True, exist_ok=True)
    scenes, frames = [], []
    for sid, start, end in scenes_frames:
        img = d / "frames" / f"{sid}.png"
        img.write_bytes(b"\x89PNG")
        scenes.append(Scene(id=sid, title=sid, start_time=start, end_time=end))
        frames.append(Frame(scene_id=sid, prompt="p", image_path=str(img)).model_dump())
    (d / "analysis.json").write_text(BookAnalysis(scenes=scenes).model_dump_json())
    (d / "manifest.json").write_text(json.dumps(frames))
    return d


def test_window_from_chunk_name():
    cues = [FrameCue(Path("a.png"), 100.0, 160.0)]
    assert _window("0000-60min", cues, 0.0) == (0.0, 3600.0)
    assert _window("0060-120min", cues, 0.0) == (3600.0, 7200.0)
    # "to end" suffix uses the audio total.
    assert _window("0060-endmin", cues, 9000.0) == (3600.0, 9000.0)
    # No window in the name -> span of the frames.
    assert _window("misc", cues, 0.0) == (100.0, 160.0)


def test_collect_segments_per_chunk(tmp_path):
    book = tmp_path / "swarm"
    _chunk(book, "0000-60min", [("a_001_00", 60.0, 120.0), ("a_001_01", 600.0, 660.0)])
    _chunk(book, "0060-120min", [("b_001_00", 3700.0, 3760.0)])

    segs = collect_segments(book, total_audio=9000.0)
    assert [s.label for s in segs] == ["0000-60min", "0060-120min"]
    # Each segment's window comes from its folder name, not the whole book.
    assert segs[0].start == 0.0 and segs[0].end == 3600.0
    assert segs[1].start == 3600.0 and segs[1].end == 7200.0
    assert len(segs[0].cues) == 2 and len(segs[1].cues) == 1


def test_frames_concat_is_window_relative():
    cues = [FrameCue(Path("/f/a.png"), 3660.0, 3720.0), FrameCue(Path("/f/b.png"), 3900.0, 3960.0)]
    text = _frames_concat(cues, w_start=3600.0, w_end=7200.0)
    # First image holds 0 -> (3900-3600)=300s; second holds 300 -> window end 3600s.
    assert "duration 300.000" in text
    assert "duration 3300.000" in text
    assert text.count("file '/f/b.png'") == 2  # last repeated for the demuxer


def test_compile_videos_renders_per_segment_and_master(tmp_path, monkeypatch):
    book = tmp_path / "swarm"
    _chunk(book, "0000-60min", [("a_001_00", 60.0, 120.0)])
    _chunk(book, "0060-120min", [("b_001_00", 3700.0, 3760.0)])

    monkeypatch.setattr(video_mod.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(video_mod, "gather_audio_files", lambda p: [Path("/audio/swarm.m4b")])
    monkeypatch.setattr(video_mod, "audio_total_duration", lambda p: 9000.0)

    calls = []

    def fake_render(seg, audio_files, fps, out, on_progress):
        calls.append((seg.label, round(seg.start), round(seg.end)))
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"MP4")

    concat = {}

    def fake_concat(paths, out):
        concat["paths"] = [Path(p).name for p in paths]
        Path(out).write_bytes(b"MASTER")

    monkeypatch.setattr(video_mod, "_render_segment", fake_render)
    monkeypatch.setattr(video_mod, "_concat_videos", fake_concat)

    segments, master = compile_videos(book, "/audio/swarm.m4b")

    # One video per chunk, each over its own window (not the whole 9000s).
    assert calls == [("0000-60min", 0, 3600), ("0060-120min", 3600, 7200)]
    assert [p.name for p in segments] == ["video.mp4", "video.mp4"]
    assert master == book / "video.mp4"
    assert concat["paths"] == ["video.mp4", "video.mp4"]


def test_run_ffmpeg_reports_progress(tmp_path, monkeypatch):
    progress = []

    class FakeProc:
        returncode = 0

        def __init__(self):
            self.stdout = iter([
                "out_time_us=1000000\n",
                "progress=continue\n",
                "out_time_us=5000000\n",
                "progress=end\n",
            ])
            self.stderr = None

        def wait(self):
            pass

    monkeypatch.setattr(video_mod.subprocess, "Popen", lambda *a, **k: FakeProc())
    video_mod._run_ffmpeg(["ffmpeg"], total=10.0, on_progress=lambda d, t: progress.append((d, t)))

    assert (1.0, 10.0) in progress
    assert (5.0, 10.0) in progress
    assert progress[-1] == (10.0, 10.0)  # finishes at 100%


def test_compile_videos_requires_frames(tmp_path, monkeypatch):
    monkeypatch.setattr(video_mod.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(video_mod, "gather_audio_files", lambda p: [Path("/a.m4b")])
    monkeypatch.setattr(video_mod, "audio_total_duration", lambda p: 100.0)
    with pytest.raises(ValueError, match="No timestamped frames"):
        compile_videos(tmp_path / "empty", "/a.m4b")


def test_single_chunk_at_book_dir_is_the_result(tmp_path, monkeypatch):
    book = tmp_path / "solo"
    _chunk(book, "", [("s1", 10.0, 20.0)])  # analysis.json directly in book dir

    monkeypatch.setattr(video_mod.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(video_mod, "gather_audio_files", lambda p: [Path("/a.m4b")])
    monkeypatch.setattr(video_mod, "audio_total_duration", lambda p: 100.0)
    monkeypatch.setattr(video_mod, "_render_segment", lambda *a: Path(a[3]).write_bytes(b"X"))

    segments, master = compile_videos(book, "/a.m4b")
    assert master == segments[0] == book / "video.mp4"
