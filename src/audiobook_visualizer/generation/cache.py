"""Content-addressed caching for generated images.

Each rendered image gets a sidecar ``<name>.json`` recording the key of the
inputs that produced it (prompt + provider + model + size + reference image
digests). On a re-run, if the image and a matching sidecar already exist, we skip
the API call. Change the style, prompt, model or a character portrait and only the
affected images regenerate.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional, Sequence


def compute_key(parts: dict[str, Any]) -> str:
    """Stable hash of the inputs that determine an image."""
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def file_digest(path: Path) -> str:
    """Short content hash of a file (used to key reference images)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def reference_digests(references: Optional[Sequence[Path]]) -> list[str]:
    out: list[str] = []
    for ref in references or []:
        ref = Path(ref)
        if ref.exists():
            out.append(file_digest(ref))
    return out


def _sidecar(image_path: Path) -> Path:
    return image_path.with_suffix(".json")


def is_cached(image_path: Path, key: str) -> bool:
    """True if ``image_path`` exists with a sidecar recording the same key."""
    if not image_path.exists():
        return False
    side = _sidecar(image_path)
    if not side.exists():
        return False
    try:
        data = json.loads(side.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return data.get("key") == key


def write_sidecar(image_path: Path, key: str, extra: Optional[dict] = None) -> None:
    data = {"key": key}
    if extra:
        data.update(extra)
    _sidecar(image_path).write_text(json.dumps(data, indent=2), encoding="utf-8")
