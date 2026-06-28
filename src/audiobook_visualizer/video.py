"""Compile generated frames + the audiobook into a timed video.

Each frame is shown from its scene's start time until the next frame's start,
synced to the audio. Works whether the audio is one file (e.g. ``.m4b``) or a
folder of parts (``.mp3``) — the parts are concatenated on the same timeline the
scene timestamps live on. Uses ffmpeg's concat demuxer; nothing is held in
memory.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .ingest import audio_total_duration, gather_audio_files
from .models import BookAnalysis

ProgressFn = Callable[[str], None]


@dataclass
class FrameCue:
    image: Path
    start: float
    end: float


def collect_frames(book_dir: str | Path) -> list[FrameCue]:
    """Gather timestamped frames across a book dir (all chunks), ordered by time.

    Reads every ``analysis.json`` under ``book_dir`` (joining each with its
    sibling ``manifest.json``) and keeps scenes that were rendered and aligned.
    """
    import json

    book_dir = Path(book_dir)
    cues: list[FrameCue] = []
    seen: set[str] = set()

    for analysis_path in sorted(book_dir.rglob("analysis.json")):
        try:
            analysis = BookAnalysis.model_validate_json(analysis_path.read_text())
        except (OSError, ValueError):
            continue
        manifest_path = analysis_path.parent / "manifest.json"
        frames_by_id: dict[str, dict] = {}
        if manifest_path.exists():
            try:
                frames_by_id = {
                    f["scene_id"]: f for f in json.loads(manifest_path.read_text())
                }
            except (OSError, ValueError, KeyError):
                frames_by_id = {}

        for scene in analysis.scenes:
            if scene.start_time is None or scene.id in seen:
                continue
            frame = frames_by_id.get(scene.id)
            if not frame or not frame.get("image_path") or frame.get("error"):
                continue
            img = _resolve_image(frame["image_path"], analysis_path.parent, scene.id)
            if img is None:
                continue
            seen.add(scene.id)
            cues.append(
                FrameCue(image=img, start=scene.start_time, end=scene.end_time or scene.start_time)
            )

    cues.sort(key=lambda c: c.start)
    return cues


def _resolve_image(image_path: str, chunk_dir: Path, scene_id: str) -> Optional[Path]:
    candidates = [
        Path(image_path),
        chunk_dir / "frames" / Path(image_path).name,
        chunk_dir / "frames" / f"{scene_id}.png",
    ]
    for cand in candidates:
        if cand.exists():
            return cand.resolve()
    return None


def compile_video(
    book_dir: str | Path,
    audio: str | Path,
    out_path: str | Path,
    fps: int = 24,
    on_progress: Optional[ProgressFn] = None,
) -> Path:
    """Render frames + audio to an mp4. Returns the output path."""
    progress = on_progress or (lambda _m: None)
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        raise RuntimeError("Compiling video requires ffmpeg/ffprobe.")

    cues = collect_frames(book_dir)
    if not cues:
        raise ValueError(
            "No timestamped frames found to compile. Run `visualize` first "
            "(frames need scene timestamps from an aligned audiobook)."
        )

    audio_files = gather_audio_files(audio)
    total = audio_total_duration(audio)
    if total <= 0:
        # Fall back to the last cue if the audio duration can't be probed.
        total = max(c.end for c in cues)
    progress(f"{len(cues)} frame(s) over {total / 60:.1f} min of audio")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="abv-video-") as workdir:
        wd = Path(workdir)
        frames_txt = wd / "frames.txt"
        frames_txt.write_text(_frames_concat(cues, total), encoding="utf-8")
        audio_txt = wd / "audio.txt"
        audio_txt.write_text(_audio_concat(audio_files), encoding="utf-8")

        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(frames_txt),
            "-f", "concat", "-safe", "0", "-i", str(audio_txt),
            "-map", "0:v", "-map", "1:a",
            "-r", str(fps), "-pix_fmt", "yuv420p", "-c:v", "libx264",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest", "-movflags", "+faststart",
            str(out_path),
        ]
        progress("Encoding video (ffmpeg)…")
        subprocess.run(cmd, check=True)
    return out_path


def _frames_concat(cues: list[FrameCue], total: float) -> str:
    """ffconcat script: hold each image until the next cue's start (then audio end)."""
    # The first image covers the lead-in from 0; each subsequent image starts at
    # its scene time.
    starts = [0.0] + [c.start for c in cues[1:]]
    lines = ["ffconcat version 1.0"]
    for i, cue in enumerate(cues):
        seg_end = starts[i + 1] if i + 1 < len(cues) else total
        duration = max(0.05, seg_end - starts[i])
        lines.append(f"file '{cue.image.as_posix()}'")
        lines.append(f"duration {duration:.3f}")
    # concat demuxer ignores the final entry's duration unless the file repeats.
    lines.append(f"file '{cues[-1].image.as_posix()}'")
    return "\n".join(lines) + "\n"


def _audio_concat(files: list[Path]) -> str:
    lines = ["ffconcat version 1.0"]
    lines += [f"file '{Path(f).resolve().as_posix()}'" for f in files]
    return "\n".join(lines) + "\n"
