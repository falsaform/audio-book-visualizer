"""OpenAI (DALL-E) image provider."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Optional, Sequence

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from ..config import require_env
from .base import ImageProvider


class OpenAIImageProvider(ImageProvider):
    name = "openai"
    # DALL-E 3 generation has no reference-image input; consistency relies on the
    # character descriptions spliced into the text prompt.
    supports_references = False

    def __init__(
        self,
        model: str = "dall-e-3",
        size: str = "1792x1024",
        quality: str = "standard",
    ) -> None:
        from openai import OpenAI

        self.model = model
        self.size = size
        self.quality = quality
        self._client = OpenAI(api_key=require_env("OPENAI_API_KEY"))

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, max=30))
    def generate(
        self,
        prompt: str,
        out_path: Path,
        references: Optional[Sequence[Path]] = None,
    ) -> Path:
        # references are ignored: DALL-E 3 generate() takes no image input.
        out_path = out_path.with_suffix(".png")
        out_path.parent.mkdir(parents=True, exist_ok=True)

        kwargs = {
            "model": self.model,
            "prompt": prompt,
            "size": self.size,
            "n": 1,
        }
        if self.model == "dall-e-3":
            kwargs["quality"] = self.quality

        resp = self._client.images.generate(**kwargs)
        datum = resp.data[0]

        # The API returns either base64 (b64_json) or a temporary URL.
        if getattr(datum, "b64_json", None):
            out_path.write_bytes(base64.b64decode(datum.b64_json))
        elif getattr(datum, "url", None):
            img = requests.get(datum.url, timeout=60)
            img.raise_for_status()
            out_path.write_bytes(img.content)
        else:  # pragma: no cover - defensive
            raise RuntimeError("OpenAI image response contained no image data")
        return out_path
