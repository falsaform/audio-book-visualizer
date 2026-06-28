"""Tests for video compilation (ffmpeg mocked)."""

import json
from pathlib import Path

import pytest

import audiobook_visualizer.video as video_mod
from audiobook_visualizer.models import BookAnalysis, Frame, Scene
from audiobook_visualizer.video import FrameCue, _frames_concat, collect_frames


def _chunk(book: Path, label: str, scenes_frames: list[tuple[str, float, float]]):
    """Create a chunk dir with analysis.json, manifest.json and frame PNGs."""
    d = book / label
    (d / "frames").mkdir(parents=True)
    scenes, frames = [], []
    for sid, start, end in scenes_frames:
        img = d / "frames" / f"{sid}.png"
        img.write_bytes(b"\x89PNG")
        scenes.append(Scene(id=sid, title=sid, start_time=start, end_time=end))
        frames.append(Frame(scene_id=sid, prompt="p", image_path=str(img)).model_dump())
    (d / "analysis.json").write_text(BookAnalysis(scenes=scenes).model_dump_json())
    (d / "manifest.json").write_text(json.dumps(frames))


def test_collect_frames_orders_across_chunks(tmp_path):
    book = tmp_path / "legion"
    _chunk(book, "0005-20min", [("b_001_00", 300.0, 360.0)])
    _chunk(book, "0000-5min", [("a_001_00", 0.0, 60.0), ("a_001_01", 120.0, 180.0)])

    cues = collect_frames(book)
    assert [round(c.start) for c in cues] == [0, 120, 300]  # sorted by time across chunks
    assert all(c.image.exists() for c in cues)


def test_collect_frames_skips_unaligned_and_failed(tmp_path):
    book = tmp_path / "b"
    d = book / "c"
    (d / "frames").mkdir(parents=True)
    img = d / "frames" / "ok.png"
    img.write_bytes(b"x")
    scenes = [
        Scene(id="ok", start_time=10.0),
        Scene(id="noimg", start_time=20.0),       # no frame
        Scene(id="notime"),                         # no timestamp
    ]
    frames = [
        Frame(scene_id="ok", prompt="p", image_path=str(img)).model_dump(),
        Frame(scene_id="noimg", prompt="p", error="429").model_dump(),
    ]
    (d / "analysis.json").write_text(BookAnalysis(scenes=scenes).model_dump_json())
    (d / "manifest.json").write_text(json.dumps(frames))

    cues = collect_frames(book)
    assert [c.image.name for c in cues] == ["ok.png"]


def test_frames_concat_durations(tmp_path):
    cues = [
        FrameCue(Path("/f/a.png"), 0.0, 60.0),
        FrameCue(Path("/f/b.png"), 120.0, 180.0),
    ]
    text = _frames_concat(cues, total=200.0)
    lines = text.splitlines()
    assert lines[0] == "ffconcat version 1.0"
    # First image holds 0->120 (next start), second holds 120->200 (audio end).
    assert "duration 120.000" in text
    assert "duration 80.000" in text
    # Last file repeated for the concat demuxer.
    assert lines.count("file '/f/b.png'") == 2


def test_compile_video_invokes_ffmpeg(tmp_path, monkeypatch):
    book = tmp_path / "legion"
    _chunk(book, "0000-5min", [("a_001_00", 0.0, 60.0)])

    monkeypatch.setattr(video_mod.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(video_mod, "gather_audio_files", lambda p: [Path("/audio/book.m4b")])
    monkeypatch.setattr(video_mod, "audio_total_duration", lambda p: 300.0)

    captured = {}

    def fake_run(cmd, check=None):
        captured["cmd"] = cmd
        Path(cmd[-1]).write_bytes(b"MP4")  # pretend ffmpeg wrote the output

    monkeypatch.setattr(video_mod.subprocess, "run", fake_run)

    out = video_mod.compile_video(book, "/audio/book.m4b", tmp_path / "out.mp4")
    assert out.exists()
    cmd = captured["cmd"]
    assert cmd[0] == "ffmpeg" and "-shortest" in cmd and str(out) == cmd[-1]


def test_compile_video_requires_frames(tmp_path, monkeypatch):
    monkeypatch.setattr(video_mod.shutil, "which", lambda name: "/usr/bin/" + name)
    with pytest.raises(ValueError, match="No timestamped frames"):
        video_mod.compile_video(tmp_path / "empty", "/a.m4b", tmp_path / "o.mp4")
