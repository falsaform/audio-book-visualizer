"""A no-network placeholder provider.

Writes a deterministic placeholder image (the prompt rendered onto a colored
card) instead of calling an API. Used by tests and by ``--dry-run`` so the full
pipeline can be exercised end-to-end without spending money or needing keys.
"""

from __future__ import annotations

import hashlib
import textwrap
from pathlib import Path

from .base import ImageProvider


class StubImageProvider(ImageProvider):
    name = "stub"
    model = "placeholder"

    def __init__(self, size: str = "1024x576") -> None:
        try:
            self.width, self.height = (int(x) for x in size.lower().split("x"))
        except ValueError:
            self.width, self.height = 1024, 576

    def generate(self, prompt: str, out_path: Path) -> Path:
        out_path = out_path.with_suffix(".png")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._render_with_pillow(prompt, out_path)
        except Exception:
            # Pillow not available: write a tiny valid PNG so downstream code
            # (gallery, manifest) still has a real file to point at.
            out_path.write_bytes(_MINIMAL_PNG)
        return out_path

    def _render_with_pillow(self, prompt: str, out_path: Path) -> None:
        from PIL import Image, ImageDraw

        # Deterministic background color derived from the prompt.
        digest = hashlib.md5(prompt.encode("utf-8")).hexdigest()
        color = (int(digest[0:2], 16), int(digest[2:4], 16), int(digest[4:6], 16))
        img = Image.new("RGB", (self.width, self.height), color)
        draw = ImageDraw.Draw(img)
        wrapped = textwrap.fill(prompt[:400], width=48)
        draw.multiline_text((24, 24), wrapped, fill=(255, 255, 255))
        draw.text((24, self.height - 28), "[stub frame]", fill=(255, 255, 255))
        img.save(out_path)


# 1x1 transparent PNG, used only if Pillow is missing.
_MINIMAL_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6360000002000154a24f5f0000000049454e44ae426082"
)
