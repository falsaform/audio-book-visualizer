"""Google Gemini image provider ("Nano Banana" = Gemini 2.5 Flash Image).

Gemini 2.5 Flash Image generates and *edits* images, and is particularly strong
at keeping a subject consistent when given reference images. We exploit that for
character consistency: a character's portrait is passed as a reference whenever
they appear in a scene, so the same face/outfit recurs across frames.
"""

from __future__ import annotations

import base64
import re
import time
from pathlib import Path
from typing import Optional, Sequence

from ..config import require_env
from .base import ImageProvider

_DEFAULT_MODEL = "gemini-2.5-flash-image"
_MAX_ATTEMPTS = 4
_MAX_BACKOFF = 60.0  # cap on how long we'll wait for a soft rate limit


class QuotaExceeded(RuntimeError):
    """Raised for a quota/rate-limit failure with a concise, actionable message."""


class GeminiImageProvider(ImageProvider):
    name = "gemini"
    supports_references = True

    def __init__(self, model: str = _DEFAULT_MODEL) -> None:
        # Imported lazily so the package imports without the SDK installed.
        from google import genai

        self.model = model or _DEFAULT_MODEL
        # The SDK also reads GEMINI_API_KEY/GOOGLE_API_KEY from the environment,
        # but we pass it explicitly for a clear error when it's missing.
        self._client = genai.Client(api_key=require_env("GEMINI_API_KEY"))

    def generate(
        self,
        prompt: str,
        out_path: Path,
        references: Optional[Sequence[Path]] = None,
    ) -> Path:
        from PIL import Image

        out_path = out_path.with_suffix(".png")
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # Reference images first, then the instruction — the model edits/extends
        # toward the prompt while preserving the referenced subjects.
        contents: list = []
        for ref in references or []:
            ref_path = Path(ref)
            if ref_path.exists():
                contents.append(Image.open(ref_path))
        contents.append(prompt)

        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = self._client.models.generate_content(
                    model=self.model, contents=contents
                )
                data = _first_image_bytes(resp)
                if data is None:
                    raise RuntimeError(
                        "Gemini returned no image (possibly blocked by safety filters)."
                    )
                out_path.write_bytes(data)
                return out_path
            except Exception as exc:  # noqa: BLE001 - classify then retry/abort
                if _is_quota_error(exc):
                    # Hard quota (free tier = 0, or daily cap) won't recover by
                    # waiting — fail fast with guidance.
                    if _is_hard_quota(exc) or attempt == _MAX_ATTEMPTS - 1:
                        raise QuotaExceeded(_quota_message(exc, _is_hard_quota(exc))) from exc
                    time.sleep(min(_retry_after(exc, 5.0 * (attempt + 1)), _MAX_BACKOFF))
                    continue
                if attempt == _MAX_ATTEMPTS - 1:
                    raise
                time.sleep(2.0 ** attempt)  # transient error: brief backoff
        raise RuntimeError("Gemini generation failed.")  # pragma: no cover


# -- error classification ----------------------------------------------------


def _is_quota_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(s in msg for s in ("429", "resource_exhausted", "quota", "rate limit"))


def _is_hard_quota(exc: Exception) -> bool:
    """Quota that waiting won't fix: zero allowance or a per-day cap."""
    msg = str(exc)
    return "limit: 0" in msg or "PerDay" in msg


def _retry_after(exc: Exception, default: float) -> float:
    """Seconds the API asks us to wait, parsed from the 429 payload."""
    msg = str(exc)
    for pat in (r"retry in ([\d.]+)s", r"retryDelay'?:?\s*'?(\d+)s"):
        m = re.search(pat, msg, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    return default


def _quota_message(exc: Exception, hard: bool) -> str:
    detail = _human_message(exc)
    if hard:
        return (
            f"Gemini quota exhausted: {detail} Your project has no image-generation "
            "quota on this tier (the free tier is often 0 for image models). Enable "
            "billing on the Google AI project, switch with --provider openai, or test "
            "the pipeline with --dry-run."
        )
    return (
        f"Gemini rate-limited: {detail} Retried but still limited — lower "
        "generation.concurrency or try again shortly."
    )


def _human_message(exc: Exception) -> str:
    """Pull the human-readable 'message' from the error, dropping the JSON noise."""
    m = re.search(r"'message':\s*'([^'\\]+)", str(exc))
    if m:
        return m.group(1).strip().rstrip(".") + "."
    return str(exc)[:160]


def _first_image_bytes(resp) -> Optional[bytes]:
    """Pull the first inline image payload from a generate_content response."""
    candidates = getattr(resp, "candidates", None) or []
    for cand in candidates:
        content = getattr(cand, "content", None)
        for part in getattr(content, "parts", None) or []:
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None) if inline else None
            if data:
                # Newer SDKs return raw bytes; older ones a base64 string.
                return data if isinstance(data, (bytes, bytearray)) else base64.b64decode(data)
    return None
