"""Audio (audiobook) transcription with timestamps.

Two backends:

* ``faster-whisper`` (default) runs locally and handles multi-hour audiobooks
  in one pass. Recommended.
* ``openai`` uses the hosted transcription API. The API caps upload size, so we
  split long files into chunks with pydub and stitch the timestamps back
  together.

Audio is optional. If transcription fails or no backend is installed, the
caller can proceed with just the ebook text.
"""

from __future__ import annotations

from pathlib import Path

from ..config import require_env
from ..models import Transcript, TranscriptSegment

# Hosted transcription API upload ceiling; we chunk below this.
_OPENAI_CHUNK_MS = 10 * 60 * 1000  # 10 minutes per chunk


def transcribe_audio(
    path: str | Path,
    backend: str = "faster-whisper",
    model: str = "base",
) -> Transcript:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    if backend == "faster-whisper":
        return _transcribe_local(path, model)
    if backend == "openai":
        return _transcribe_openai(path)
    raise ValueError(f"Unknown audio backend: {backend!r}")


def _transcribe_local(path: Path, model: str) -> Transcript:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "faster-whisper is not installed. Install it (`pip install "
            "faster-whisper`) or set audio.backend: openai in your config."
        ) from exc

    whisper = WhisperModel(model, device="auto", compute_type="auto")
    segments, info = whisper.transcribe(str(path), vad_filter=True)
    result = [
        TranscriptSegment(start=seg.start, end=seg.end, text=seg.text.strip())
        for seg in segments
    ]
    return Transcript(language=getattr(info, "language", None), segments=result)


def _transcribe_openai(path: Path) -> Transcript:
    from openai import OpenAI

    try:
        from pydub import AudioSegment
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "pydub is required to chunk audio for the OpenAI backend "
            "(`pip install pydub`, plus ffmpeg)."
        ) from exc

    client = OpenAI(api_key=require_env("OPENAI_API_KEY"))
    audio = AudioSegment.from_file(path)

    segments: list[TranscriptSegment] = []
    for offset_ms in range(0, len(audio), _OPENAI_CHUNK_MS):
        chunk = audio[offset_ms : offset_ms + _OPENAI_CHUNK_MS]
        tmp = path.with_suffix(f".chunk{offset_ms}.mp3")
        chunk.export(tmp, format="mp3")
        try:
            with open(tmp, "rb") as fh:
                resp = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=fh,
                    response_format="verbose_json",
                    timestamp_granularities=["segment"],
                )
        finally:
            tmp.unlink(missing_ok=True)

        offset_s = offset_ms / 1000.0
        for seg in getattr(resp, "segments", []) or []:
            segments.append(
                TranscriptSegment(
                    start=seg["start"] + offset_s,
                    end=seg["end"] + offset_s,
                    text=seg["text"].strip(),
                )
            )
    return Transcript(language=getattr(audio, "language", None), segments=segments)
