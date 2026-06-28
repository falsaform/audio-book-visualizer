"""Google Gemini image provider ("Nano Banana" = Gemini 2.5 Flash Image).

Gemini 2.5 Flash Image generates and *edits* images, and is particularly strong
at keeping a subject consistent when given reference images. We exploit that for
character consistency: a character's portrait is passed as a reference whenever
they appear in a scene, so the same face/outfit recurs across frames.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Optional, Sequence

from tenacity import retry, stop_after_attempt, wait_exponential

from ..config import require_env
from .base import ImageProvider

_DEFAULT_MODEL = "gemini-2.5-flash-image"


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

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, max=30),
        reraise=True,
    )
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

        resp = self._client.models.generate_content(model=self.model, contents=contents)
        data = _first_image_bytes(resp)
        if data is None:
            raise RuntimeError(
                "Gemini returned no image (possibly blocked by safety filters)."
            )
        out_path.write_bytes(data)
        return out_path


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
