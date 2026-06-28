"""Build an image-generation prompt from a scene.

The key idea for *consistency* across frames: whenever a scene names a known
character, we splice that character's canonical appearance (from the character
bible) into the prompt. So "Ahab stands at the helm" becomes a prompt that also
says exactly what Ahab looks like — every single time he appears.
"""

from __future__ import annotations

from ..models import BookAnalysis, Character, Scene

# DALL-E and similar models reliably refuse to render legible text; we ask for
# none to avoid garbled captions in frames.
_NEGATIVE = "No text, no words, no captions, no watermark, no signature."

# Headroom so subtle camera motion (Ken Burns) in the video never crops faces.
_FRAMING = (
    "Compose with margin and headroom: keep characters fully within the frame, "
    "heads and faces well clear of the edges."
)


def build_prompt(
    scene: Scene,
    analysis: BookAnalysis,
    style: str,
    include_shot: bool = True,
    with_references: bool = False,
) -> str:
    """Compose the image prompt for a scene.

    ``with_references`` signals that the provider will also receive character
    portrait images, so the prompt nudges it to keep the referenced characters
    consistent (the textual descriptions are still included as a fallback).
    """
    parts: list[str] = []

    if scene.shot_type and include_shot:
        parts.append(f"{scene.shot_type}.")

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
    if with_references and appearances:
        parts.append(
            "Keep the characters consistent with the provided reference portraits."
        )

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

    parts.append(_FRAMING)
    parts.append(_NEGATIVE)
    return " ".join(p.strip() for p in parts if p.strip())


def build_portrait_prompt(character: Character, style: str) -> str:
    """A reference-portrait prompt for one character (used for consistency)."""
    parts = [
        f"Character reference portrait of {character.name}.",
        character.description,
        "Head-and-shoulders, facing forward, plain neutral studio background,"
        " even lighting, consistent character design sheet.",
    ]
    if style:
        parts.append(f"Style: {style}.")
    parts.append(_NEGATIVE)
    return " ".join(p.strip() for p in parts if p.strip())
