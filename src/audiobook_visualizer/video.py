"""Compile generated frames + the audiobook into timed videos.

Each rendered chunk becomes its **own** segment video covering only that chunk's
time window (its frames placed at their timestamps, with the matching slice of
audio). A **master** video then concatenates the segments. Works whether the
audio is one file (``.m4b``) or a folder of parts (``.mp3``) — the parts share
one timeline, and each segment seeks its window out of it. Uses ffmpeg's concat
demuxer; nothing is held in memory, and encode progress is reported live.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .ingest import audio_total_duration, gather_audio_files
from .store import ProductionStore

LogFn = Callable[[str], None]
ProgressFn = Callable[[float, float], None]  # (seconds_done, seconds_total)


@dataclass
class Motion:
    """Ken Burns settings. Defaults are crop-safe: a gentle zoom *out* that
    ends on the full, uncropped frame, anchored slightly high to protect heads."""

    zoom: float = 1.08          # max zoom factor (1.0 = no zoom)
    style: str = "out"          # "out" | "in" | "alternate"
    top_bias: float = 0.3       # 0=top-aligned, 0.5=center (keeps heads in frame)


@dataclass
class FrameCue:
    image: Path
    start: float
    end: float
    move: str = ""  # per-shot camera move (director mode); "" = global Ken Burns


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
    """One :class:`Segment` per rendered chunk, read from the production store.

    Each segment's time window is the span persisted on its :class:`Segment` row
    (what the chunk folder name used to encode); cues come from its shots' frames.
    """
    book_dir = Path(book_dir)
    store = ProductionStore.open(book_dir)
    production_id = store.get_production_id()
    if production_id is None:
        return []
    view = store.production_view(production_id)
    if view is None:
        return []

    segments: list[Segment] = []
    for seg in view.segments:
        cues: list[FrameCue] = []
        for shot in seg.shots:
            if shot.start_time is None:
                continue
            frame = shot.frame
            if not frame or not frame.image_path or frame.error:
                continue
            chunk_dir = book_dir if seg.chunk_label == "full" else book_dir / seg.chunk_label
            img = _resolve_image(frame.image_path, chunk_dir, shot.slug)
            if img is None:
                continue
            cues.append(FrameCue(
                img, shot.start_time, shot.end_time or shot.start_time, shot.camera_move
            ))
        if not cues:
            continue
        cues.sort(key=lambda c: c.start)

        chunk_dir = book_dir if seg.chunk_label == "full" else book_dir / seg.chunk_label
        label = "full" if seg.chunk_label == "full" else seg.chunk_label
        start, end = seg.start, seg.end
        if end <= start:  # open-ended/undated window -> the span the frames cover
            end = max(c.end for c in cues)
        if total_audio:
            end = min(end, total_audio)
        segments.append(Segment(chunk_dir, label, start, end, cues))

    segments.sort(key=lambda s: s.start)
    return segments


def _resolve_image(image_path: str, chunk_dir: Path, slug: str) -> Optional[Path]:
    for cand in (
        Path(image_path),
        chunk_dir / "frames" / Path(image_path).name,
        chunk_dir / "frames" / f"{slug}.png",
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
    motion: Optional[Motion] = None,
    force: bool = False,
    on_log: Optional[LogFn] = None,
    on_progress: Optional[ProgressFn] = None,
) -> tuple[list[Path], Optional[Path]]:
    """Render one video per chunk + a master joining them. Returns (segments, master).

    Each segment video carries a fingerprint of its inputs (the frame images and
    the render settings). A segment is re-rendered only when that fingerprint
    changes — i.e. a frame was regenerated, added/removed, or an option like
    ``fps``/``motion`` differs. Pass ``force`` to rebuild everything regardless.
    The master is rebuilt whenever any segment changed (or ``force``).
    """
    log = on_log or (lambda _m: None)
    motion = motion or Motion()
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

    master = book_dir / "video.mp4"
    outputs: list[Path] = []
    rebuilt = False
    for seg in segments:
        out = seg.chunk_dir / "video.mp4"
        fp_path = seg.chunk_dir / "video.fingerprint"
        fingerprint = _segment_fingerprint(seg, fps, fade, ken_burns, motion)
        # Reuse the existing segment video only if its inputs are unchanged.
        # The fingerprint covers the frame images (size + mtime) and the render
        # settings, so a regenerated frame or a different option forces a rebuild.
        if out.exists() and not force and _read_text(fp_path) == fingerprint:
            log(f"Segment {seg.label}: unchanged, reusing existing video")
            outputs.append(out)
            continue
        log(
            f"Segment {seg.label}: {len(seg.cues)} frame(s), "
            f"{_fmt(seg.start)}–{_fmt(seg.end)} ({seg.duration / 60:.1f} min)"
        )
        _render_segment(seg, audio_files, fps, fade, ken_burns, motion, out, on_progress, log)
        fp_path.write_text(fingerprint, encoding="utf-8")
        outputs.append(out)
        rebuilt = True

    # The single segment already lives at the book dir -> it is the result.
    if len(outputs) == 1 and segments[0].chunk_dir == book_dir:
        return outputs, outputs[0]
    # Rebuild the master only when a segment changed (the concat is otherwise stale-free).
    if master.exists() and not rebuilt and not force:
        log("All segments unchanged; master is up to date")
        return outputs, master
    log(f"Joining {len(outputs)} segment(s) -> master")
    _concat_videos(outputs, master)
    return outputs, master


def _segment_fingerprint(
    seg: Segment, fps: int, fade: float, ken_burns: bool, motion: Motion
) -> str:
    """Digest of everything that determines a segment video: its frame images
    (size + mtime, so a regenerated frame shows up), their timings, and the
    render settings. Stored next to the video as ``video.fingerprint``."""
    h = hashlib.sha256()
    h.update(
        f"v2|fps={fps}|fade={fade}|kb={ken_burns}|"
        f"zoom={motion.zoom}|style={motion.style}|bias={motion.top_bias}|"
        f"win={seg.start:.3f}-{seg.end:.3f}\n".encode()
    )
    for cue in seg.cues:
        h.update(f"{cue.image.name}|{cue.start:.3f}|{cue.end:.3f}|{cue.move}|".encode())
        try:
            st = cue.image.stat()
            h.update(f"{st.st_size}|{st.st_mtime_ns}\n".encode())
        except OSError:
            h.update(b"missing\n")
    return h.hexdigest()


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _render_segment(
    seg: Segment,
    audio_files: list[Path],
    fps: int,
    fade: float,
    ken_burns: bool,
    motion: Motion,
    out: Path,
    on_progress: Optional[ProgressFn],
    log: LogFn,
) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    if ken_burns:
        try:
            _render_kenburns(seg, audio_files, fps, fade, motion, out, on_progress)
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
    seg: Segment, audio_files: list[Path], fps: int, fade: float, motion: Motion,
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
                "-vf", _camera_move_vf(cue.move, i, frames, fps, w, h, motion),
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


def _camera_move_vf(
    move: str, idx: int, frames: int, fps: int, w: int, h: int, motion: Motion
) -> str:
    """An ffmpeg ``zoompan`` filter realising one shot's camera move.

    An empty move (legacy scenes/paragraphs content) delegates to the global
    :func:`_ken_burns_vf`. Director shots carry an explicit move:
    ``push_in``/``pull_out`` zoom; ``pan_*``/``track_*`` travel horizontally and
    ``tilt_*`` vertically at a slight zoom (so there is room to move); ``static``
    holds the full, uncropped frame.
    """
    move = (move or "").strip().lower()
    if move in ("", "kenburns", "ken_burns"):
        return _ken_burns_vf(idx, frames, fps, w, h, motion)

    up_w, up_h = w * 2, h * 2
    dn = max(1, frames - 1)
    top = motion.top_bias
    max_zoom = max(1.01, motion.zoom)
    pz = max(max_zoom, 1.15)  # pan/tilt need crop room to travel within
    centered_x = "(iw-iw/zoom)/2"
    biased_y = f"(ih-ih/zoom)*{top:.3f}"

    if move == "push_in":
        inc = (max_zoom - 1.0) / dn
        z, x, y = f"min(1.0+{inc:.6f}*on,{max_zoom:.4f})", centered_x, biased_y
    elif move == "pull_out":
        inc = (max_zoom - 1.0) / dn
        z = f"if(eq(on,0),{max_zoom:.4f},max(zoom-{inc:.6f},1.0))"
        x, y = centered_x, biased_y
    elif move in ("pan_left", "pan_right", "track_left", "track_right"):
        rng = f"(iw-iw/{pz:.4f})"
        moving_right = move.endswith("_right")
        z = f"{pz:.4f}"
        x = f"{rng}*(on/{dn})" if moving_right else f"{rng}*(1-on/{dn})"
        y = f"(ih-ih/{pz:.4f})*{top:.3f}"
    elif move in ("tilt_up", "tilt_down"):
        rng = f"(ih-ih/{pz:.4f})"
        z = f"{pz:.4f}"
        x = f"(iw-iw/{pz:.4f})/2"
        y = f"{rng}*(on/{dn})" if move == "tilt_down" else f"{rng}*(1-on/{dn})"
    else:  # "static" (or anything unexpected): a still hold of the full frame
        z, x, y = "1.0", centered_x, biased_y

    return _zoompan(up_w, up_h, w, h, frames, fps, z, x, y)


def _zoompan(up_w: int, up_h: int, w: int, h: int, frames: int, fps: int,
             z: str, x: str, y: str) -> str:
    return (
        f"scale={up_w}:{up_h}:force_original_aspect_ratio=increase,"
        f"crop={up_w}:{up_h},"
        f"zoompan=z='{z}':d={frames}:x='{x}':y='{y}':"
        f"s={w}x{h}:fps={fps},format=yuv420p"
    )


def _ken_burns_vf(idx: int, frames: int, fps: int, w: int, h: int, motion: Motion) -> str:
    """A crop-safe subtle zoom for one still. Upscale first to keep zoompan smooth.

    Default style "out" starts slightly zoomed and ends at exactly 1.0, so the
    shot resolves to the *complete* frame — heads/subjects are never left cropped.
    The window is anchored slightly high (``top_bias``) to keep heads in view even
    during the zoomed portion.
    """
    max_zoom = max(1.0, motion.zoom)
    inc = (max_zoom - 1.0) / max(1, frames)
    up_w, up_h = w * 2, h * 2

    style = motion.style
    if style == "alternate":
        style = "in" if idx % 2 == 0 else "out"
    if style == "in" and max_zoom > 1.0:
        z = f"min(zoom+{inc:.6f},{max_zoom:.4f})"
    elif max_zoom > 1.0:  # "out": start zoomed, settle on the full frame
        z = f"if(eq(on,0),{max_zoom:.4f},max(zoom-{inc:.6f},1.0))"
    else:
        z = "1.0"

    x = "(iw-iw/zoom)/2"  # centered horizontally
    y = f"(ih-ih/zoom)*{motion.top_bias:.3f}"  # biased toward the top (heads)
    return (
        f"scale={up_w}:{up_h}:force_original_aspect_ratio=increase,"
        f"crop={up_w}:{up_h},"
        f"zoompan=z='{z}':d={frames}:x='{x}':y='{y}':"
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
