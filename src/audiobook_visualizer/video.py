"""Compile generated frames + the audiobook into timed videos.

Each rendered chunk becomes its **own** segment video covering only that chunk's
time window (its frames placed at their timestamps, with the matching slice of
audio). A **master** video then concatenates the segments. Works whether the
audio is one file (``.m4b``) or a folder of parts (``.mp3``) — the parts share
one timeline, and each segment seeks its window out of it. Uses ffmpeg's concat
demuxer; nothing is held in memory, and encode progress is reported live.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .ingest import audio_total_duration, gather_audio_files
from .models import BookAnalysis

LogFn = Callable[[str], None]
ProgressFn = Callable[[float, float], None]  # (seconds_done, seconds_total)

_WINDOW_RE = re.compile(r"(\d+)-(\d+)min")
_WINDOW_END_RE = re.compile(r"(\d+)-endmin")


@dataclass
class FrameCue:
    image: Path
    start: float
    end: float


@dataclass
class Segment:
    chunk_dir: Path
    label: str
    start: float
    end: float
    cues: list[FrameCue] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


# -- discovery ---------------------------------------------------------------


def collect_segments(book_dir: str | Path, total_audio: float = 0.0) -> list[Segment]:
    """One :class:`Segment` per rendered chunk, with its time window and frames.

    The window comes from the chunk folder name when it encodes one (e.g.
    ``0000-60min``); otherwise it falls back to the span of the chunk's frames.
    """
    book_dir = Path(book_dir)
    segments: list[Segment] = []

    for analysis_path in sorted(book_dir.rglob("analysis.json")):
        cues = _chunk_cues(analysis_path)
        if not cues:
            continue
        chunk_dir = analysis_path.parent
        label = chunk_dir.name if chunk_dir != book_dir else "full"
        start, end = _window(chunk_dir.name, cues, total_audio)
        segments.append(Segment(chunk_dir, label, start, end, cues))

    segments.sort(key=lambda s: s.start)
    return segments


def _chunk_cues(analysis_path: Path) -> list[FrameCue]:
    try:
        analysis = BookAnalysis.model_validate_json(analysis_path.read_text())
    except (OSError, ValueError):
        return []
    manifest_path = analysis_path.parent / "manifest.json"
    frames_by_id: dict[str, dict] = {}
    if manifest_path.exists():
        try:
            frames_by_id = {f["scene_id"]: f for f in json.loads(manifest_path.read_text())}
        except (OSError, ValueError, KeyError):
            frames_by_id = {}

    cues: list[FrameCue] = []
    for scene in analysis.scenes:
        if scene.start_time is None:
            continue
        frame = frames_by_id.get(scene.id)
        if not frame or not frame.get("image_path") or frame.get("error"):
            continue
        img = _resolve_image(frame["image_path"], analysis_path.parent, scene.id)
        if img is None:
            continue
        cues.append(FrameCue(img, scene.start_time, scene.end_time or scene.start_time))
    cues.sort(key=lambda c: c.start)
    return cues


def _window(name: str, cues: list[FrameCue], total_audio: float) -> tuple[float, float]:
    """Time window for a chunk: from its folder name, else its frame span."""
    if m := _WINDOW_RE.search(name):
        return float(m.group(1)) * 60.0, float(m.group(2)) * 60.0
    if m := _WINDOW_END_RE.search(name):
        start = float(m.group(1)) * 60.0
        return start, (total_audio if total_audio else max(c.end for c in cues))
    # Fall back to the span the frames cover.
    start = min(c.start for c in cues)
    end = max(c.end for c in cues)
    if total_audio:
        end = min(end, total_audio)
    return start, end


def _resolve_image(image_path: str, chunk_dir: Path, scene_id: str) -> Optional[Path]:
    for cand in (
        Path(image_path),
        chunk_dir / "frames" / Path(image_path).name,
        chunk_dir / "frames" / f"{scene_id}.png",
    ):
        if cand.exists():
            return cand.resolve()
    return None


# -- compilation -------------------------------------------------------------


def compile_videos(
    book_dir: str | Path,
    audio: str | Path,
    fps: int = 24,
    fade: float = 0.5,
    ken_burns: bool = True,
    on_log: Optional[LogFn] = None,
    on_progress: Optional[ProgressFn] = None,
) -> tuple[list[Path], Optional[Path]]:
    """Render one video per chunk + a master joining them. Returns (segments, master)."""
    log = on_log or (lambda _m: None)
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        raise RuntimeError("Compiling video requires ffmpeg/ffprobe.")

    book_dir = Path(book_dir)
    audio_files = gather_audio_files(audio)
    total_audio = audio_total_duration(audio)

    segments = collect_segments(book_dir, total_audio)
    if not segments:
        raise ValueError(
            "No timestamped frames found to compile. Run `visualize` first "
            "(frames need scene timestamps from an aligned audiobook)."
        )
    log(f"{len(segments)} segment(s) with frames")

    outputs: list[Path] = []
    for seg in segments:
        out = seg.chunk_dir / "video.mp4"
        log(
            f"Segment {seg.label}: {len(seg.cues)} frame(s), "
            f"{_fmt(seg.start)}–{_fmt(seg.end)} ({seg.duration / 60:.1f} min)"
        )
        _render_segment(seg, audio_files, fps, fade, ken_burns, out, on_progress, log)
        outputs.append(out)

    # Master: concat the segment videos. If the only segment already lives at the
    # book dir, it is the result.
    if len(outputs) == 1 and segments[0].chunk_dir == book_dir:
        return outputs, outputs[0]
    master = book_dir / "video.mp4"
    log(f"Joining {len(outputs)} segment(s) -> master")
    _concat_videos(outputs, master)
    return outputs, master


def _render_segment(
    seg: Segment,
    audio_files: list[Path],
    fps: int,
    fade: float,
    ken_burns: bool,
    out: Path,
    on_progress: Optional[ProgressFn],
    log: LogFn,
) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    if ken_burns:
        try:
            _render_kenburns(seg, audio_files, fps, fade, out, on_progress)
            return
        except Exception as exc:  # noqa: BLE001 - never let a filter quirk break it
            log(f"  (Ken Burns failed, using static frames: {exc})")
    _render_static(seg, audio_files, fps, fade, out, on_progress)


def _render_static(
    seg: Segment, audio_files: list[Path], fps: int, fade: float,
    out: Path, on_progress: Optional[ProgressFn],
) -> None:
    """Hold each still for its duration (no motion)."""
    win = seg.duration
    with tempfile.TemporaryDirectory(prefix="abv-video-") as workdir:
        wd = Path(workdir)
        frames_txt = wd / "frames.txt"
        frames_txt.write_text(_frames_concat(seg.cues, seg.start, seg.end), encoding="utf-8")
        audio_txt = wd / "audio.txt"
        audio_txt.write_text(_audio_concat(audio_files), encoding="utf-8")
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-progress", "pipe:1", "-nostats",
            "-f", "concat", "-safe", "0", "-i", str(frames_txt),
            "-ss", f"{seg.start:.3f}", "-t", f"{win:.3f}",
            "-f", "concat", "-safe", "0", "-i", str(audio_txt),
            "-map", "0:v", "-map", "1:a",
            # Drive CFR with the fps filter — reliably holds each still for its
            # full duration (a bare -r drops/flashes the first image).
            "-vf", _video_filter(fps, win, fade),
            "-c:v", "libx264", "-c:a", "aac", "-b:a", "192k",
            "-shortest", "-movflags", "+faststart", str(out),
        ]
        _run_ffmpeg(cmd, win, on_progress)


def _render_kenburns(
    seg: Segment, audio_files: list[Path], fps: int, fade: float,
    out: Path, on_progress: Optional[ProgressFn],
) -> None:
    """Slow zoom/pan (Ken Burns) per still, then concat + mux audio."""
    win = seg.duration
    durations = _cue_durations(seg.cues, seg.start, seg.end)
    w, h = _image_size(seg.cues[0].image)

    with tempfile.TemporaryDirectory(prefix="abv-video-") as workdir:
        wd = Path(workdir)
        clips: list[Path] = []
        done = 0.0
        for i, (cue, dur) in enumerate(zip(seg.cues, durations)):
            clip = wd / f"clip_{i:05d}.mp4"
            frames = max(1, round(dur * fps))
            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-loop", "1", "-i", str(cue.image),
                "-vf", _ken_burns_vf(i, frames, fps, w, h),
                "-frames:v", str(frames), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-an", str(clip),
            ]
            subprocess.run(cmd, check=True, capture_output=True)
            clips.append(clip)
            done += dur
            if on_progress and win:
                on_progress(min(done, win), win)

        clips_txt = wd / "clips.txt"
        clips_txt.write_text(
            "ffconcat version 1.0\n"
            + "\n".join(f"file '{c.as_posix()}'" for c in clips) + "\n",
            encoding="utf-8",
        )
        audio_txt = wd / "audio.txt"
        audio_txt.write_text(_audio_concat(audio_files), encoding="utf-8")

        # Concat the motion clips + mux the audio window. Re-encode video only if
        # fades are requested; otherwise stream-copy (fast).
        vcodec = ["-c:v", "libx264", "-vf", _fade_filter(win, fade)] if fade > 0 \
            else ["-c:v", "copy"]
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(clips_txt),
            "-ss", f"{seg.start:.3f}", "-t", f"{win:.3f}",
            "-f", "concat", "-safe", "0", "-i", str(audio_txt),
            "-map", "0:v", "-map", "1:a", *vcodec,
            "-c:a", "aac", "-b:a", "192k",
            "-shortest", "-movflags", "+faststart", str(out),
        ]
        subprocess.run(cmd, check=True, capture_output=True)


def _video_filter(fps: int, win: float, fade: float) -> str:
    chain = f"fps={fps},format=yuv420p"
    fadeflt = _fade_filter(win, fade)
    return f"{chain},{fadeflt}" if fadeflt else chain


def _fade_filter(win: float, fade: float) -> str:
    if fade > 0 and win > 2.5 * fade:
        return f"fade=t=in:st=0:d={fade:.3f},fade=t=out:st={win - fade:.3f}:d={fade:.3f}"
    return ""


def _ken_burns_vf(idx: int, frames: int, fps: int, w: int, h: int) -> str:
    """A subtle zoom (alternating in/out) for one still. Upscale first to keep
    zoompan smooth (it works in integer output pixels)."""
    max_zoom = 1.12
    inc = (max_zoom - 1.0) / max(1, frames)
    up_w, up_h = w * 2, h * 2
    if idx % 2 == 0:  # slow zoom in
        z = f"min(zoom+{inc:.6f},{max_zoom:.3f})"
    else:  # slow zoom out (start zoomed, ease back toward 1.0)
        z = f"if(eq(on,0),{max_zoom:.3f},max(zoom-{inc:.6f},1.0))"
    return (
        f"scale={up_w}:{up_h}:force_original_aspect_ratio=increase,"
        f"crop={up_w}:{up_h},"
        f"zoompan=z='{z}':d={frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"s={w}x{h}:fps={fps},format=yuv420p"
    )


def _cue_durations(cues: list[FrameCue], w_start: float, w_end: float) -> list[float]:
    win = max(0.0, w_end - w_start)
    starts = [0.0] + [max(0.0, c.start - w_start) for c in cues[1:]]
    durs = []
    for i in range(len(cues)):
        seg_end = starts[i + 1] if i + 1 < len(cues) else win
        durs.append(max(0.2, seg_end - starts[i]))
    return durs


def _image_size(path: Path) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(path) as img:
            return img.size
    except Exception:  # noqa: BLE001
        return 1792, 1024  # widescreen default


def _concat_videos(segment_paths: list[Path], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="abv-video-") as workdir:
        list_txt = Path(workdir) / "segments.txt"
        list_txt.write_text(
            "ffconcat version 1.0\n"
            + "\n".join(f"file '{Path(p).resolve().as_posix()}'" for p in segment_paths)
            + "\n",
            encoding="utf-8",
        )
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(list_txt),
            "-c", "copy", "-movflags", "+faststart", str(out),
        ]
        _run_ffmpeg(cmd, 0.0, None)


def _run_ffmpeg(cmd: list[str], total: float, on_progress: Optional[ProgressFn]) -> None:
    """Run ffmpeg, streaming `-progress` output to ``on_progress``."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.stdout is not None:
        for line in proc.stdout:
            if on_progress and total and line.startswith("out_time_us="):
                try:
                    secs = int(line.split("=", 1)[1]) / 1_000_000
                    on_progress(min(secs, total), total)
                except ValueError:
                    pass
    proc.wait()
    if proc.returncode != 0:
        err = proc.stderr.read() if proc.stderr else ""
        raise RuntimeError(f"ffmpeg failed (exit {proc.returncode}): {err.strip()[:500]}")
    if on_progress and total:
        on_progress(total, total)


# -- concat scripts ----------------------------------------------------------


def _frames_concat(cues: list[FrameCue], w_start: float, w_end: float) -> str:
    """ffconcat: each image held until the next cue (timeline relative to window)."""
    win = max(0.0, w_end - w_start)
    starts = [0.0] + [max(0.0, c.start - w_start) for c in cues[1:]]
    lines = ["ffconcat version 1.0"]
    for i, cue in enumerate(cues):
        seg_end = starts[i + 1] if i + 1 < len(cues) else win
        duration = max(0.05, seg_end - starts[i])
        lines.append(f"file '{cue.image.as_posix()}'")
        lines.append(f"duration {duration:.3f}")
    lines.append(f"file '{cues[-1].image.as_posix()}'")  # repeat last for the demuxer
    return "\n".join(lines) + "\n"


def _audio_concat(files: list[Path]) -> str:
    return (
        "ffconcat version 1.0\n"
        + "\n".join(f"file '{Path(f).resolve().as_posix()}'" for f in files)
        + "\n"
    )


def _fmt(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"
