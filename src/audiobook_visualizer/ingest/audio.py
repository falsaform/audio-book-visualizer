"""Audio (audiobook) transcription with timestamps.

Long audiobooks (12+ hours) do not fit in memory once decoded, so we never load
a whole file. Instead we stream it through ffmpeg into small on-disk chunks
(``chunk_seconds`` each, re-encoded to 16 kHz mono — what Whisper wants anyway),
transcribe one chunk at a time, delete it, and stitch the per-chunk timestamps
back together with a running offset. Peak memory and temp disk stay bounded to a
single chunk no matter how long the book is.

Two backends:

* ``faster-whisper`` (default) runs locally; the model is loaded once and reused
  across chunks.
* ``openai`` uses the hosted transcription API; each chunk is one request (and
  comfortably under the API's upload-size limit).

Chunking needs ffmpeg/ffprobe (present in the Docker image). If they are missing
or the duration can't be probed, we fall back to a single whole-file pass.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Iterator, Optional

from ..config import require_env
from ..models import Transcript, TranscriptSegment

# Progress callback: (seconds_done, seconds_total). Total may be 0 if unknown.
ProgressCb = Callable[[float, float], None]

# Audio file extensions recognized when a directory of parts is given.
AUDIO_EXTS = {
    ".mp3", ".m4a", ".m4b", ".wav", ".flac", ".aac", ".ogg", ".oga", ".opus", ".wma",
}

# A "timeline" entry: one file placed on the global timeline.
TimelineItem = tuple[Path, float, float]  # (file, start_offset, duration)


def _natural_key(path: Path):
    """Sort key so 'Part 2' precedes 'Part 10' (split-audiobook ordering)."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", path.name)]


def gather_audio_files(path: str | Path) -> list[Path]:
    """Resolve a file or a directory-of-parts to an ordered list of audio files."""
    p = Path(path)
    if p.is_dir():
        files = [f for f in p.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_EXTS]
        if not files:
            raise FileNotFoundError(f"No audio files found in directory: {p}")
        return sorted(files, key=_natural_key)
    if not p.exists():
        raise FileNotFoundError(p)
    return [p]


def _build_timeline(files: list[Path]) -> tuple[list[TimelineItem], float]:
    """Lay files end to end on one timeline; return (items, total_duration)."""
    timeline: list[TimelineItem] = []
    cursor = 0.0
    for f in files:
        dur = _probe_duration(f)
        timeline.append((f, cursor, dur))
        cursor += dur
    return timeline, cursor


def audio_total_duration(path: str | Path) -> float:
    """Total duration (seconds) of a file or a folder of parts (via ffprobe)."""
    return sum(_probe_duration(f) for f in gather_audio_files(path))


def audio_file_boundaries(path: str | Path) -> Optional[list[tuple[str, float, float]]]:
    """Chapter boundaries from a multi-file audiobook (one chapter per file).

    Returns ``(title, start, end)`` per file, or ``None`` for a single file (so
    single files keep using marker/heading/time chapter detection).
    """
    files = gather_audio_files(path)
    if len(files) < 2:
        return None
    timeline, _total = _build_timeline(files)
    return [(f.stem, start, start + dur) for f, start, dur in timeline]


def transcribe_audio(
    path: str | Path,
    backend: str = "faster-whisper",
    model: str = "base",
    on_progress: Optional[ProgressCb] = None,
    chunk_seconds: int = 600,
    start: float = 0.0,
    duration: Optional[float] = None,
) -> Transcript:
    """Transcribe ``path`` — a single file OR a directory of audio parts.

    Multiple files are placed end to end on one timeline, so timestamps are
    continuous across parts. ``start``/``duration`` (seconds) optionally restrict
    transcription to a window of that timeline; timestamps stay absolute so a
    preview matches a full run. Windows and multi-file input require ffmpeg.
    """
    files = gather_audio_files(path)
    multi = len(files) > 1
    have_ffmpeg = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))

    if multi and not have_ffmpeg:
        raise RuntimeError("Multi-file (folder) audio requires ffmpeg/ffprobe.")

    if have_ffmpeg:
        timeline, total = _build_timeline(files)
    else:
        timeline, total = [(files[0], 0.0, 0.0)], 0.0  # single-file, no probe

    w_start = max(0.0, float(start or 0.0))
    windowed = w_start > 0.0 or duration is not None
    if windowed and not have_ffmpeg:
        raise RuntimeError("Previewing a time window requires ffmpeg/ffprobe.")
    if windowed and total and w_start >= total:
        raise ValueError(
            f"--start ({w_start / 60:.1f} min) is beyond the audio "
            f"length ({total / 60:.1f} min)."
        )
    w_end = (w_start + float(duration)) if duration is not None else total
    if total:
        w_end = min(w_end, total) if w_end else total

    # Chunking is required to window or to walk multiple files; else optional.
    can_chunk = bool(chunk_seconds and total and have_ffmpeg)
    chunk = (chunk_seconds or 600) if (windowed or multi) else (chunk_seconds if can_chunk else 0)

    if backend == "faster-whisper":
        return _transcribe_local(timeline, model, on_progress, w_start, w_end, chunk)
    if backend == "openai":
        return _transcribe_openai(timeline, on_progress, w_start, w_end, chunk)
    raise ValueError(f"Unknown audio backend: {backend!r}")


# -- backends ---------------------------------------------------------------


def _transcribe_local(
    timeline: list[TimelineItem],
    model: str,
    on_progress: Optional[ProgressCb],
    w_start: float,
    w_end: float,
    chunk_seconds: int,
) -> Transcript:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "faster-whisper is not installed. Install it (`pip install "
            "faster-whisper`) or set audio.backend: openai in your config."
        ) from exc

    whisper = WhisperModel(model, device="auto", compute_type="auto")
    segments: list[TranscriptSegment] = []
    language: Optional[str] = None
    window_len = (w_end - w_start) if w_end else 0.0

    def transcribe_part(part: Path, offset: float) -> None:
        nonlocal language
        seg_iter, info = whisper.transcribe(str(part), vad_filter=True)
        language = language or getattr(info, "language", None)
        # Progress spans the window; fall back to this file's duration if unknown.
        span = window_len or float(getattr(info, "duration", 0.0) or 0.0)
        for seg in seg_iter:
            segments.append(
                TranscriptSegment(
                    start=seg.start + offset,
                    end=seg.end + offset,
                    text=seg.text.strip(),
                )
            )
            if on_progress and span:
                done = (seg.end + offset - w_start) if window_len else seg.end + offset
                on_progress(min(done, span), span)

    _drive(timeline, w_start, w_end, chunk_seconds, transcribe_part)
    if on_progress and window_len:
        on_progress(window_len, window_len)
    return Transcript(language=language, segments=segments)


def _transcribe_openai(
    timeline: list[TimelineItem],
    on_progress: Optional[ProgressCb],
    w_start: float,
    w_end: float,
    chunk_seconds: int,
) -> Transcript:
    from openai import OpenAI

    client = OpenAI(api_key=require_env("OPENAI_API_KEY"))
    segments: list[TranscriptSegment] = []
    window_len = (w_end - w_start) if w_end else 0.0

    def transcribe_part(part: Path, offset: float) -> None:
        with open(part, "rb") as fh:
            resp = client.audio.transcriptions.create(
                model="whisper-1",
                file=fh,
                response_format="verbose_json",
                timestamp_granularities=["segment"],
            )
        for seg in getattr(resp, "segments", None) or []:
            segments.append(
                TranscriptSegment(
                    start=_seg_get(seg, "start") + offset,
                    end=_seg_get(seg, "end") + offset,
                    text=_seg_get(seg, "text").strip(),
                )
            )
        if on_progress and window_len:
            done = min(offset - w_start + (chunk_seconds or window_len), window_len)
            on_progress(done, window_len)

    _drive(timeline, w_start, w_end, chunk_seconds, transcribe_part)
    if on_progress and window_len:
        on_progress(window_len, window_len)
    return Transcript(segments=segments)


# -- chunk driver ------------------------------------------------------------


def _drive(
    timeline: list[TimelineItem],
    w_start: float,
    w_end: float,
    chunk_seconds: int,
    handle: Callable[[Path, float], None],
) -> None:
    """Feed ``handle(part_path, global_offset)`` per-chunk across the timeline.

    Each file contributes the portion that overlaps ``[w_start, w_end)``; chunk
    offsets are translated to the global timeline so timestamps stay continuous.
    """
    if not chunk_seconds:
        handle(timeline[0][0], 0.0)  # single-file, no-ffmpeg fallback
        return
    with tempfile.TemporaryDirectory(prefix="abv-audio-") as workdir:
        wd = Path(workdir)
        for file, file_off, dur in timeline:
            file_end = file_off + dur
            seg_start = max(w_start, file_off)
            seg_end = min(w_end, file_end) if w_end else file_end
            if seg_end <= seg_start:
                continue  # this file is outside the window
            within_start = seg_start - file_off
            within_end = seg_end - file_off
            for part, within in _iter_chunks(file, within_start, within_end, chunk_seconds, wd):
                handle(part, file_off + within)


def _iter_chunks(
    path: Path, w_start: float, w_end: float, chunk_seconds: int, workdir: Path
) -> Iterator[tuple[Path, float]]:
    """Extract the window ``[w_start, w_end)`` one chunk at a time.

    Yields ``(chunk_path, offset)`` where ``offset`` is the chunk's absolute
    position in the original file, then deletes the chunk. Lazy extraction (one
    ffmpeg call per chunk with fast input seeking) keeps temp disk bounded to a
    single chunk, not the whole re-encoded region.
    """
    idx = 0
    pos = w_start
    while pos < w_end:
        length = min(float(chunk_seconds), w_end - pos)
        out = workdir / f"chunk_{idx:05d}.wav"
        _extract_chunk(path, pos, length, out)
        if out.exists() and out.stat().st_size > 0:
            try:
                yield out, pos
            finally:
                out.unlink(missing_ok=True)
        pos += chunk_seconds
        idx += 1


# -- ffmpeg / ffprobe helpers ------------------------------------------------


def _extract_chunk(path: Path, start: float, length: float, out: Path) -> None:
    """Cut ``[start, start+length)`` to 16 kHz mono WAV via ffmpeg (streaming)."""
    # -ss before -i = fast input seek; -vn drops m4b cover art.
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-ss", f"{start:.3f}", "-i", str(path), "-t", f"{length:.3f}",
            "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(out),
        ],
        check=True,
    )


def _probe_duration(path: Path) -> float:
    """Return audio duration in seconds via ffprobe, or 0.0 if unavailable."""
    if not shutil.which("ffprobe"):
        return 0.0
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                "-of", "csv=p=0", str(path),
            ],
            capture_output=True, text=True, timeout=120,
        )
    except (subprocess.SubprocessError, OSError):
        return 0.0
    try:
        return float(proc.stdout.strip())
    except (TypeError, ValueError):
        return 0.0


def _seg_get(seg: object, key: str):
    """Read a field from an OpenAI segment (dict in older SDKs, object in newer)."""
    if isinstance(seg, dict):
        return seg[key]
    return getattr(seg, key)
