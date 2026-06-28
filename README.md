# Audiobook Visualizer

Turn an **audiobook** and/or **ebook** into AI-analyzed scenes and AI-generated
**still frames** — a visual storyboard of the book.

The pipeline reads the source, uses **Claude** to build a character bible and
identify the most visually compelling moments, then uses **OpenAI DALL·E** to
render each moment as an image. Characters look consistent across frames because
each character's canonical appearance is injected into every prompt they appear
in. Output is a folder of frames plus a browsable HTML gallery.

```
 ingest                 analyze (Claude)              generate (DALL·E)
┌────────────┐   text   ┌─────────────────────┐  ┌────────────────────┐
│ .epub/.pdf │ ───────► │ character bible      │  │ build prompt       │
│ .txt       │          │ scene / visual-      │► │ (+character bible) │ ► frames/*.png
│ audiobook  │ ──┐      │   moment extraction  │  │ DALL·E image       │   gallery.html
└────────────┘   │ ts   └─────────────────────┘  └────────────────────┘   manifest.json
                 └────────► align scenes to audio timestamps (optional)
```

## Status

This is a working **MVP skeleton**. The full pipeline runs end-to-end, including
a `--dry-run` mode that needs no API keys (it renders placeholder frames so you
can validate the flow). See [Roadmap](#roadmap) for what's intentionally simple.

## Tooling

Everything runs in **Docker** via **docker compose**, orchestrated by a
**justfile**. Package management is **uv** (the lockfile is `uv.lock`). You only
need Docker, `docker compose`, and [`just`](https://github.com/casey/just)
installed on the host — Python, ffmpeg and all dependencies live in the image.

## Install

```bash
cp .env.example .env   # fill in ANTHROPIC_API_KEY and OPENAI_API_KEY
just build             # build the docker image (installs deps via uv)
```

`just` on its own lists every recipe:

```bash
just            # show all recipes
just test       # run the test suite in the container
just demo       # offline dry-run on the bundled sample (stub frames)
just shell      # open a shell in the container
```

## Quickstart

All commands run in the container; pass CLI flags straight through `just`:

```bash
# Full run: ebook + audiobook -> analyzed scenes -> generated frames -> gallery
just visualize --ebook book.epub --audio book.m4b

# Ebook only, just the analysis (characters + scenes), no images
just visualize --ebook book.pdf --analyze-only

# Render placeholder frames without calling the image API (still calls Claude)
just visualize --ebook book.epub --dry-run --max-frames 6

# Inspect the character bible from a finished run
just abv characters output/analysis.json
```

Put your input files in the project directory (it is bind-mounted into the
container at `/app`) and reference them by relative path. Output lands in
`./output/`; open `output/gallery.html` when it finishes.

### Managing dependencies

```bash
just lock       # re-resolve uv.lock after editing pyproject.toml, then rebuild
```

### Useful flags

| Flag | Purpose |
|------|---------|
| `--ebook / -e` | Path to `.pdf` / `.epub` / `.txt`. |
| `--audio / -a` | Path to audiobook (`.mp3` / `.m4a` / `.m4b` / `.wav`). |
| `--analyze-only` | Stop after analysis; write `analysis.json`, skip images. |
| `--dry-run` | Use the offline stub image provider (no DALL·E calls). |
| `--style / -s` | Override the visual style applied to every frame. |
| `--max-frames / -n` | Cap how many frames are generated. |
| `--out / -o` | Output directory. |
| `--config / -c` | Path to a `config.yaml`. |

## Configuration

Everything has sane defaults. To tune, copy `config.example.yaml` to
`config.yaml` (auto-discovered) and edit. Highlights:

- `project.style` — the single biggest lever on how frames look.
- `analysis.model` — which Claude model analyzes the text.
- `audio.backend` — `faster-whisper` (local, default) or `openai` (hosted API).
- `generation.model` / `size` / `quality` — DALL·E settings.
- `generation.max_frames` / `concurrency` — cost and speed controls.

## Outputs

A run writes to `output/` (configurable):

- `analysis.json` — characters + scenes (the structured analysis).
- `frames/*.png` — one image per scene.
- `manifest.json` — each frame's prompt, provider and resulting file.
- `gallery.html` — a self-contained gallery to browse the frames.

## How it works

1. **Ingest** (`ingest/`) — extract chapter-aware text from PDF/EPUB/TXT;
   optionally transcribe the audiobook to timestamped segments.
2. **Analyze** (`analysis/`) — chunk the text; ask Claude to (a) build a
   character bible of physical appearances and (b) pick key visual moments,
   each with a setting, mood, characters present and a rich visual description.
3. **Align** (`analysis/align.py`) — fuzzy-match each scene's quoted excerpt to
   the transcript to attach audio timestamps (best-effort).
4. **Generate** (`generation/`) — build a prompt per scene, splicing in the
   canonical appearance of every character present (for consistency), and render
   it with the configured image provider.
5. **Gallery** (`gallery.py`) — assemble frames + metadata into HTML.

## Extending

- **New image backend** (Replicate, local Stable Diffusion, Gemini): implement
  `ImageProvider.generate` in `generation/`, register it in
  `generation/factory.py`. Nothing else changes.
- **Better prompts**: edit `analysis/prompts.py` (analysis) and
  `generation/prompt_builder.py` (image prompt assembly).

## Roadmap

Deliberately simple in this MVP, good next steps:

- True forced alignment (e.g. WhisperX) instead of fuzzy excerpt matching.
- Reference-image / character-portrait conditioning for stronger consistency.
- Per-chapter pacing controls and shot-type variety (wide/close-up).
- A small web UI for browsing/regenerating individual frames.
- Caching so re-runs only regenerate changed scenes.

## Testing

```bash
just test            # run everything
just test -k align   # pass args straight through to pytest
```

The suite covers ingestion, analysis helpers, prompt building, alignment, and
the full generation + gallery stages using the offline stub provider — no API
keys required.
