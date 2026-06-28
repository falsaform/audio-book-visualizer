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

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Iterator, Optional

from ..config import require_env
from ..models import Transcript, TranscriptSegment

# Progress callback: (seconds_done, seconds_total). Total may be 0 if unknown.
ProgressCb = Callable[[float, float], None]


def transcribe_audio(
    path: str | Path,
    backend: str = "faster-whisper",
    model: str = "base",
    on_progress: Optional[ProgressCb] = None,
    chunk_seconds: int = 600,
    start: float = 0.0,
    duration: Optional[float] = None,
) -> Transcript:
    """Transcribe ``path`` (optionally only the window ``[start, start+duration]``).

    ``start``/``duration`` are in seconds; timestamps in the result stay absolute
    (relative to the original file) so a preview's times match a full run. A
    window requires ffmpeg/ffprobe.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    have_ffmpeg = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
    total = _probe_duration(path)

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

    # Chunking is required to extract a window; otherwise it's an optimization.
    can_chunk = bool(chunk_seconds and total and have_ffmpeg)
    if windowed:
        chunk = chunk_seconds or 600
    else:
        chunk = chunk_seconds if can_chunk else 0

    if backend == "faster-whisper":
        return _transcribe_local(path, model, on_progress, w_start, w_end, chunk)
    if backend == "openai":
        return _transcribe_openai(path, on_progress, w_start, w_end, chunk)
    raise ValueError(f"Unknown audio backend: {backend!r}")


# -- backends ---------------------------------------------------------------


def _transcribe_local(
    path: Path,
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

    _drive(path, w_start, w_end, chunk_seconds, transcribe_part)
    if on_progress and window_len:
        on_progress(window_len, window_len)
    return Transcript(language=language, segments=segments)


def _transcribe_openai(
    path: Path,
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

    _drive(path, w_start, w_end, chunk_seconds, transcribe_part)
    if on_progress and window_len:
        on_progress(window_len, window_len)
    return Transcript(segments=segments)


# -- chunk driver ------------------------------------------------------------


def _drive(
    path: Path,
    w_start: float,
    w_end: float,
    chunk_seconds: int,
    handle: Callable[[Path, float], None],
) -> None:
    """Feed ``handle(part_path, offset)`` per-chunk over the window, or whole-file."""
    if not chunk_seconds:
        handle(path, 0.0)
        return
    with tempfile.TemporaryDirectory(prefix="abv-audio-") as workdir:
        for part, offset in _iter_chunks(path, w_start, w_end, chunk_seconds, Path(workdir)):
            handle(part, offset)


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
