"""Tests for per-segment video compilation (ffmpeg mocked)."""

from pathlib import Path

import pytest

import audiobook_visualizer.video as video_mod
from audiobook_visualizer.video import (
    FrameCue,
    _frames_concat,
    collect_segments,
    compile_videos,
)


def _seg(label, start, end, frames):
    """A seed-spec segment whose scenes all have rendered frames."""
    return {
        "label": label, "start": start, "end": end,
        "scenes": [
            {"slug": sid, "start": s, "end": e, "frame": True}
            for sid, s, e in frames
        ],
    }


def test_segment_window_from_store(tmp_path, store_seeder):
    book = tmp_path / "swarm"
    store_seeder(book, [_seg("0000-60min", 0.0, 3600.0, [("a_001_00", 60.0, 120.0)])])
    segs = collect_segments(book, total_audio=9000.0)
    # The window now comes from the persisted Segment row (not the folder name).
    assert segs[0].start == 0.0 and segs[0].end == 3600.0


def test_collect_segments_per_chunk(tmp_path, store_seeder):
    book = tmp_path / "swarm"
    store_seeder(book, [
        _seg("0000-60min", 0.0, 3600.0, [("a_001_00", 60.0, 120.0), ("a_001_01", 600.0, 660.0)]),
        _seg("0060-120min", 3600.0, 7200.0, [("b_001_00", 3700.0, 3760.0)]),
    ])

    segs = collect_segments(book, total_audio=9000.0)
    assert [s.label for s in segs] == ["0000-60min", "0060-120min"]
    # Each segment's window comes from its own row, not the whole book.
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


def test_compile_videos_renders_per_segment_and_master(tmp_path, monkeypatch, store_seeder):
    book = tmp_path / "swarm"
    store_seeder(book, [
        _seg("0000-60min", 0.0, 3600.0, [("a_001_00", 60.0, 120.0)]),
        _seg("0060-120min", 3600.0, 7200.0, [("b_001_00", 3700.0, 3760.0)]),
    ])

    monkeypatch.setattr(video_mod.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(video_mod, "gather_audio_files", lambda p: [Path("/audio/swarm.m4b")])
    monkeypatch.setattr(video_mod, "audio_total_duration", lambda p: 9000.0)

    calls = []

    def fake_render(seg, audio_files, fps, fade, ken_burns, motion, out, on_progress, log):
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


def test_video_filter_uses_fps_and_fade():
    from audiobook_visualizer.video import _video_filter

    # The fps filter (not a bare -r) drives CFR so the first still doesn't flash.
    vf = _video_filter(24, win=600.0, fade=0.5)
    assert vf.startswith("fps=24,format=yuv420p")
    assert "fade=t=in:st=0:d=0.500" in vf
    assert "fade=t=out:st=599.500:d=0.500" in vf
    # Fade skipped for very short windows / fade=0.
    assert "fade" not in _video_filter(24, win=1.0, fade=0.5)
    assert "fade" not in _video_filter(24, win=600.0, fade=0.0)


def test_cue_durations_window_relative():
    from audiobook_visualizer.video import _cue_durations

    cues = [FrameCue(Path("a"), 3660.0, 3720.0), FrameCue(Path("b"), 3900.0, 3960.0)]
    durs = _cue_durations(cues, w_start=3600.0, w_end=7200.0)
    assert durs == [300.0, 3300.0]  # first 0->300, second 300->window end (3600)


def test_ken_burns_default_is_crop_safe_zoom_out():
    from audiobook_visualizer.video import Motion, _ken_burns_vf

    vf = _ken_burns_vf(0, frames=240, fps=24, w=1792, h=1024, motion=Motion())
    assert "zoompan" in vf and "s=1792x1024" in vf
    assert "scale=3584:2048" in vf       # upscaled 2x to keep zoompan smooth
    assert "max(zoom-" in vf             # default "out" -> settles on the full frame
    assert "*0.300" in vf                # top-biased anchor (heads protected)


def test_ken_burns_alternate_and_no_zoom():
    from audiobook_visualizer.video import Motion, _ken_burns_vf

    even = _ken_burns_vf(0, 240, 24, 1792, 1024, Motion(style="alternate"))
    odd = _ken_burns_vf(1, 240, 24, 1792, 1024, Motion(style="alternate"))
    assert "min(zoom+" in even   # even -> in
    assert "max(zoom-" in odd    # odd  -> out
    # zoom=1.0 -> a still hold (no crop at all).
    assert "z='1.0'" in _ken_burns_vf(0, 240, 24, 1792, 1024, Motion(zoom=1.0))


def test_render_segment_falls_back_when_ken_burns_fails(tmp_path, monkeypatch):
    from audiobook_visualizer.video import Segment, _render_segment

    seg = Segment(tmp_path, "0000-60min", 0.0, 60.0, [FrameCue(tmp_path / "a.png", 10.0, 20.0)])
    msgs = []

    def boom(*a, **k):
        raise RuntimeError("zoompan exploded")

    static_called = {}

    def fake_static(*a, **k):
        static_called["yes"] = True

    monkeypatch.setattr(video_mod, "_render_kenburns", boom)
    monkeypatch.setattr(video_mod, "_render_static", fake_static)

    from audiobook_visualizer.video import Motion

    _render_segment(seg, [], 24, 0.5, True, Motion(), tmp_path / "o.mp4", None, msgs.append)
    assert static_called.get("yes")  # fell back to static
    assert any("Ken Burns failed" in m for m in msgs)


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


def test_segment_video_reused_until_frames_change(tmp_path, monkeypatch, store_seeder):
    book = tmp_path / "swarm"
    store_seeder(book, [
        _seg("0000-60min", 0.0, 3600.0, [("a_001_00", 60.0, 120.0)]),
        _seg("0060-120min", 3600.0, 7200.0, [("b_001_00", 3700.0, 3760.0)]),
    ])

    monkeypatch.setattr(video_mod.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(video_mod, "gather_audio_files", lambda p: [Path("/audio/swarm.m4b")])
    monkeypatch.setattr(video_mod, "audio_total_duration", lambda p: 9000.0)

    rendered: list[str] = []

    def fake_render(seg, audio_files, fps, fade, ken_burns, motion, out, on_progress, log):
        rendered.append(seg.label)
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"MP4")

    monkeypatch.setattr(video_mod, "_render_segment", fake_render)
    monkeypatch.setattr(video_mod, "_concat_videos", lambda paths, out: Path(out).write_bytes(b"M"))

    # First pass renders both segments and writes a fingerprint for each.
    compile_videos(book, "/audio/swarm.m4b")
    assert sorted(rendered) == ["0000-60min", "0060-120min"]
    assert (book / "0000-60min" / "video.fingerprint").exists()

    # Second pass: nothing changed -> no re-render.
    rendered.clear()
    compile_videos(book, "/audio/swarm.m4b")
    assert rendered == []

    # Regenerate one frame -> only that segment is rebuilt.
    rendered.clear()
    frame = book / "0000-60min" / "frames" / "a_001_00.png"
    frame.write_bytes(b"\x89PNG-new-bytes-bigger")  # changed size -> new fingerprint
    compile_videos(book, "/audio/swarm.m4b")
    assert rendered == ["0000-60min"]

    # force re-renders everything regardless of fingerprints.
    rendered.clear()
    compile_videos(book, "/audio/swarm.m4b", force=True)
    assert sorted(rendered) == ["0000-60min", "0060-120min"]


def test_single_chunk_at_book_dir_is_the_result(tmp_path, monkeypatch, store_seeder):
    book = tmp_path / "solo"
    store_seeder(book, [_seg("full", 10.0, 20.0, [("s1", 10.0, 20.0)])])  # one "full" segment

    monkeypatch.setattr(video_mod.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(video_mod, "gather_audio_files", lambda p: [Path("/a.m4b")])
    monkeypatch.setattr(video_mod, "audio_total_duration", lambda p: 100.0)
    monkeypatch.setattr(video_mod, "_render_segment", lambda *a: Path(a[6]).write_bytes(b"X"))

    segments, master = compile_videos(book, "/a.m4b")
    assert master == segments[0] == book / "video.mp4"
