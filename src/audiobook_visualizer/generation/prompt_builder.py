"""Build an image-generation prompt from a scene.

The key idea for *consistency* across frames: whenever a scene names a known
character, we splice that character's canonical appearance (from the character
bible) into the prompt. So "Ahab stands at the helm" becomes a prompt that also
says exactly what Ahab looks like — every single time he appears.
"""

from __future__ import annotations

from ..models import BookAnalysis, Scene

# DALL-E and similar models reliably refuse to render legible text; we ask for
# none to avoid garbled captions in frames.
_NEGATIVE = "No text, no words, no captions, no watermark, no signature."


def build_prompt(scene: Scene, analysis: BookAnalysis, style: str) -> str:
    parts: list[str] = []

    if scene.visual_description:
        parts.append(scene.visual_description)
    elif scene.summary:
        parts.append(scene.summary)

    # Inject canonical appearances for any named characters present.
    appearances = []
    for mention in scene.characters_present:
        char = analysis.character(mention)
        if char and char.description:
            appearances.append(f"{char.name}: {char.description}")
    if appearances:
        parts.append("Character appearances — " + " ".join(appearances))

    # Scene framing hints.
    framing = []
    if scene.setting:
        framing.append(scene.setting)
    if scene.time_of_day:
        framing.append(scene.time_of_day)
    if scene.mood:
        framing.append(f"{scene.mood} mood")
    if framing:
        parts.append("Setting: " + ", ".join(framing) + ".")

    if style:
        parts.append(f"Style: {style}.")

    parts.append(_NEGATIVE)
    return " ".join(p.strip() for p in parts if p.strip())
