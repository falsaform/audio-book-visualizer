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

# Audiobook-only: no ebook needed — structure is recovered from the audio
just visualize --audio book.m4b

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

### Audiobook-only mode

With no ebook, the pipeline recovers structure from the audio itself —
transcribing it and segmenting into **chapters** and **paragraphs** so each
piece can be processed (and timestamped) individually. Chapter boundaries are
detected, in order of preference:

1. **Embedded markers** — `.m4b` chapter tables, read via `ffprobe`.
2. **Spoken headings** — the narrator saying "Chapter One", etc.
3. **Time windows** — a fixed-length fallback (`chapter_seconds`).

Paragraphs are split on the narration's natural pauses landing on sentence
boundaries (`paragraph_gap`). Tune any of this under `audio:` in `config.yaml`
or with `--chapter-mode auto|markers|headings|time|single`.

You can run **just** the segmentation — transcription only, no API keys — to
inspect or pre-process the structure:

```bash
just segment --audio book.m4b          # writes output/audiobook_structure.json
```

Transcription is the slow step, so `segment` shows a live **progress bar** that
advances with the audio position (with elapsed/remaining time). `visualize`
reports the same progress as throttled percentage lines.

`audiobook_structure.json` contains every chapter and paragraph with its audio
start/end times. A full `visualize` run on an audiobook writes the same file
alongside `analysis.json`.

### Claude Code in the container

The image ships the [Claude Code](https://claude.com/claude-code) CLI. Authenticate
it with a `CLAUDE_CODE_OAUTH_TOKEN` (generate one with `claude setup-token` on a
machine that has Claude Code; put it in `.env`, or export it in CI):

```bash
just claude                       # interactive session in the container
just claude -p "explain src/audiobook_visualizer/pipeline.py"
```

The token is forwarded into the container from `.env` or the host environment by
docker compose.

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
| `--chapter-mode` | Audiobook-only chapter detection: `auto`/`markers`/`headings`/`time`/`single`. |
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
- `audiobook_structure.json` — chapters + paragraphs with audio timestamps (audiobook-only mode).
- `frames/*.png` — one image per scene.
- `manifest.json` — each frame's prompt, provider and resulting file.
- `gallery.html` — a self-contained gallery to browse the frames.

## How it works

1. **Ingest** (`ingest/`) — extract chapter-aware text from PDF/EPUB/TXT;
   optionally transcribe the audiobook to timestamped segments. In audiobook-only
   mode, `segmentation.py` recovers chapters (markers/headings/time) and
   paragraphs (narration pauses) from the transcript.
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

The suite covers ingestion, analysis helpers, prompt building, alignment,
audiobook chapter/paragraph segmentation, and the full generation + gallery
stages using the offline stub provider — no API keys required.

CI (`.github/workflows/ci.yml`) builds the Docker image and runs `just lint`
and `just test` in the container on every pull request and on pushes to `main`.
