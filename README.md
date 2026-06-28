# Audiobook Visualizer

Turn an **audiobook** and/or **ebook** into AI-analyzed scenes and AI-generated
**still frames** — a visual storyboard of the book.

The pipeline reads the source, uses **Claude** to build a character bible and
identify the most visually compelling moments, then renders each moment as an
image with a pluggable provider — **OpenAI DALL·E** or **Google Gemini 2.5 Flash
Image** ("Nano Banana"). Characters stay consistent across frames: their
canonical appearance is injected into every prompt, and with Gemini a generated
character **portrait** is also passed as a reference image. Output is a folder of
frames plus a browsable HTML gallery.

```
 ingest                 analyze (Claude)              generate (DALL·E)
┌────────────┐   text   ┌─────────────────────┐  ┌────────────────────┐
│ .epub/.pdf │ ───────► │ character bible      │  │ build prompt       │
│ .txt       │          │ scene / visual-      │► │ (+character bible) │ ► frames/*.png
│ audiobook  │ ──┐      │   moment extraction  │  │ DALL·E image       │   gallery.html
└────────────┘   │ ts   └─────────────────────┘  └────────────────────┘   production.db
                 └────────► align scenes to audio timestamps (optional)
```

Characters, scenes, shots and frames are persisted to a per-book **`production.db`**
(SQLite, via SQLModel) — the single source of truth that the video compiler, web UI
and gallery all read from. Frame images stay as files on disk; the database stores
their paths.

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
cp .env.example .env   # add your credentials (see below)
just build             # build the docker image (installs deps via uv)
```

**Credentials.** Text analysis needs *one* Claude credential; image generation
needs an OpenAI key:

- `ANTHROPIC_API_KEY` **or** `CLAUDE_CODE_OAUTH_TOKEN` — for character/scene
  analysis. If you only have a Claude Code subscription token, set
  `CLAUDE_CODE_OAUTH_TOKEN` (generate it with `claude setup-token`); analysis is
  routed through the bundled `claude` CLI, no API key required. If both are set,
  the API key wins. Override the choice with `analysis.provider` in `config.yaml`.
- `OPENAI_API_KEY` (for DALL·E) **or** `GEMINI_API_KEY` (for Gemini / Nano Banana).
  With `generation.provider: auto` (the default) the backend is chosen from
  whichever key is set, so just provide one. `OPENAI_API_KEY` is also used for
  optional OpenAI audio transcription.

`just` on its own lists every recipe:

```bash
just            # show all recipes
just test       # run the test suite in the container
just demo       # offline dry-run on the bundled sample (stub frames)
just shell      # open a shell in the container
```

> **File ownership.** `just` runs the container as your host user/group
> (`--user $(id -u):$(id -g)`), so everything written to `output/` is owned by
> **you** — you can browse the `production.db` and delete frames without `sudo`.
> This needs a Unix host (Linux/macOS/WSL). If you built an earlier image, run
> `just down` once to drop the old root-owned cache volumes, then `just build`.

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
just abv characters output
```

Put your input files in the project directory (it is bind-mounted into the
container at `/app`) and reference them by relative path. Output lands in
`./output/`; open `output/gallery.html` when it finishes.

### Audiobook-only mode

With no ebook, the pipeline recovers structure from the audio itself —
transcribing it and segmenting into **chapters** and **paragraphs** so each
piece can be processed (and timestamped) individually. Chapter boundaries are
detected, in order of preference:

1. **Per-file** — a *folder* of audio parts (see below): one chapter per file.
2. **Embedded markers** — `.m4b` chapter tables, read via `ffprobe`.
3. **Spoken headings** — the narrator saying "Chapter One", etc.
4. **Time windows** — a fixed-length fallback (`chapter_seconds`).

**Split audiobooks (a folder of mp3s).** Point `--audio` at a *directory* and
the parts are placed end to end on one timeline (sorted naturally, so `Part 2`
precedes `Part 10`) with continuous timestamps. Each file becomes a chapter
(titled from its filename) — the usual layout for split audiobooks:

```bash
just segment   --audio "audiobooks/Moby Dick/"   # folder of NN - Chapter.mp3 files
just visualize --audio "audiobooks/Moby Dick/"
```

Paragraphs are split on the narration's natural pauses landing on sentence
boundaries (`paragraph_gap`). Tune any of this under `audio:` in `config.yaml`
or with `--chapter-mode auto|markers|headings|time|single`.

You can run **just** the segmentation — transcription only, no API keys — to
inspect or pre-process the structure:

```bash
just segment --audio book.m4b          # writes output/audiobook_structure.json
```

**Preview a slice to iterate fast.** Transcribing a 12h book just to check your
segmentation tweaks is painful, so `segment` takes a `--start`/`--duration`
window **in minutes**. Only that slice is extracted (via ffmpeg seeking) and
transcribed; timestamps stay absolute (relative to the original file), so the
preview matches what a full run would produce for that region:

```bash
just segment --audio book.m4b -d 10              # first 10 minutes
just segment --audio book.m4b --start 60 -d 15   # minutes 60–75
```

**Process in slices, then join.** For a very long book you can segment it in
windows (even on different machines) and stitch the pieces back together —
windows keep absolute timestamps, so the join just orders by time, and a chapter
split across a window boundary is rejoined:

```bash
just segment --audio book.m4b --start 0  -d 60   # -> .../audiobook_structure_0000-60min.json
just segment --audio book.m4b --start 60 -d 60   # -> .../audiobook_structure_0060-120min.json
just abv join output/book/audiobook_structure_*min.json -o output/book/audiobook_structure.json
just visualize --structure output/book/audiobook_structure.json
```

Windowed segments are written to distinct `audiobook_structure_<start>-<end>min.json`
files (so they don't overwrite each other); a full-file segment writes plain
`audiobook_structure.json`.

Transcription is the slow step, so `segment` shows a live **progress bar** that
advances with the audio position (with elapsed/remaining time). `visualize`
reports the same progress as throttled percentage lines.

**Long audiobooks (12h+) stay memory-safe.** Rather than decoding the whole file
into RAM, the audio is streamed through ffmpeg into small on-disk chunks
(`audio.chunk_seconds`, default 600s), transcribed one at a time, and stitched
back together with running timestamps. Peak memory and temp disk are bounded to a
single chunk regardless of book length. Set `chunk_seconds: 0` to disable (only
for tiny inputs).

`audiobook_structure.json` contains every chapter and paragraph with its audio
start/end times. A full `visualize` run on an audiobook writes this file at the
book level, alongside `production.db`.

**Reuse the structure (skip re-transcribing).** Once you're happy with a
`segment` result, feed it straight into the full pipeline — `visualize` will use
it for analysis and frame generation instead of transcribing the audio again:

```bash
just visualize --structure output/audiobook_structure.json
```

This is the fast path for long books (transcribe once, then iterate on analysis
and frames), and it pairs with the preview window: segment a 10-minute slice,
then `visualize --structure` it for a quick end-to-end preview.

### Rendering chunks incrementally (no overwrite, shared characters)

You can render a long book in pieces and accumulate the results. Each structure
file renders into **its own subfolder** (named from the structure filename), so
chunks never overwrite each other — while the **character bible and portraits are
shared at the book level** and reused across chunks:

```bash
just visualize --structure output/legion/audiobook_structure_0000-5min.json
just visualize --structure output/legion/audiobook_structure_0005-20min.json
```
```
output/legion/
  production.db              # the source of truth (characters, scenes, shots, frames)
  portraits/<name>_<hash>.png  # reference portraits, cached & shared
  0000-5min/                 # chunk: frames/, gallery.html, video.mp4
  0005-20min/
```

- **Continuity:** each chunk seeds analysis with the accumulated character bible
  in `production.db`, so recurring characters stay consistent (and scene ids are
  namespaced per chunk).
- **Caching across chunks:** portraits are keyed by an appearance hash. The same
  look is reused (no re-generation, no wasted quota); a character whose look
  *changes* over the book gets a new portrait — so evolving appearances are kept.

### Claude Code in the container

The image ships the [Claude Code](https://claude.com/claude-code) CLI. Authenticate
it with a `CLAUDE_CODE_OAUTH_TOKEN` (generate one with `claude setup-token` on a
machine that has Claude Code; put it in `.env`, or export it in CI):

```bash
just claude                       # interactive session in the container
just claude -p "explain src/audiobook_visualizer/pipeline.py"
```

The token is forwarded into the container from `.env` or the host environment by
docker compose. The same token also powers the **analysis stage** when no
`ANTHROPIC_API_KEY` is set (see [Install](#install)) — the pipeline calls this
CLI under the hood, so you can run the whole thing with just a Claude Code
subscription token plus an OpenAI key.

### Managing dependencies

```bash
just lock       # re-resolve uv.lock after editing pyproject.toml, then rebuild
```

### Useful flags

| Flag | Purpose |
|------|---------|
| `--ebook / -e` | Path to `.pdf` / `.epub` / `.txt`. |
| `--audio / -a` | Path to audiobook (`.mp3` / `.m4a` / `.m4b` / `.wav`). |
| `--structure` | Reuse an existing `audiobook_structure.json` (skip transcription). |
| `--analyze-only` | Stop after analysis; persist scenes to `production.db`, skip images. |
| `--dry-run` | Use the offline stub image provider (no DALL·E/Gemini calls). |
| `--provider / -p` | Image provider: `openai` / `gemini` / `stub`. |
| `--force` | Regenerate all frames, ignoring the cache. |
| `--chapter-mode` | Audiobook-only chapter detection: `auto`/`markers`/`headings`/`time`/`single`. |
| `--style / -s` | Override the visual style applied to every frame. |
| `--max-frames / -n` | Cap how many frames are generated. |
| `--out / -o` | Output directory. |
| `--config / -c` | Path to a `config.yaml`. |

## Image providers & consistency

`generation.provider` defaults to `auto` — it picks `openai` if `OPENAI_API_KEY`
is set, else `gemini` if `GEMINI_API_KEY` is set. Force one with
`generation.provider` or `--provider`:

| Provider | Model | Reference images | Notes |
|----------|-------|------------------|-------|
| `auto`   | — | — | Pick from available keys (default). |
| `openai` | `dall-e-3` | ✗ | Consistency via text descriptions only. |
| `gemini` | `gemini-2.5-flash-image` ("Nano Banana") | ✓ | Strongest character consistency. Needs **billing** (free tier is often 0 for image models). |
| `stub`   | — | ✓ | Offline placeholder cards (`--dry-run`). |

> Gemini image generation typically requires **billing enabled** on your Google
> AI project — the free tier quota for image models is frequently `0`. If you hit
> a `429 RESOURCE_EXHAUSTED` with `limit: 0`, that's the cause: enable billing,
> switch with `--provider openai`, or test the pipeline offline with `--dry-run`.
> For low rate limits, lower `generation.concurrency`.

**Character portraits.** When the provider supports reference images (Gemini),
the pipeline first renders one **reference portrait per character** that appears,
then passes the relevant portraits alongside each scene prompt — so the same
face/outfit recurs across frames. Portraits land in `output/portraits/` and are
cached. Toggle with `generation.character_portraits`.

**Edit-and-regenerate (delete to redo).** Re-running keeps any frame or portrait
whose file already exists, and reuses a segment that's already in `production.db`
instead of re-analyzing. So the iteration loop is:

1. Run once. Browse the gallery (or the **web UI**) and the frames.
2. Tweak a single frame's prompt/style in the web UI, or `--reanalyze` to rebuild
   the whole segment's analysis from the source.
3. **Delete** the frames/portraits you want redone.
4. Re-run — only the missing ones regenerate.

`--reanalyze` forces a fresh analysis; `--force` regenerates every image even if
it exists; `generation.cache: false` disables the keep-existing behavior.
Portraits are square (`generation.portrait_size`, default `1024x1024`) so the
face isn't cropped; frames use the widescreen `generation.size`.

**Shot variety.** The analyzer tags each moment with a shot type (wide
establishing / medium / close-up), folded into the prompt for visual rhythm.
Toggle with `generation.shot_variety`.

```bash
just visualize --audio book.m4b --provider gemini   # Nano Banana, with portraits
just visualize --ebook book.epub --force            # rebuild every frame
```

## Web UI

Browse the frames and **regenerate any one of them individually** — tweak its
prompt or swap a style, click regenerate, and only that image rebuilds (through
the same pipeline + cache):

```bash
just web                       # serves http://localhost:8000 on the configured output dir
just web --out runs/moby       # browse a specific run
just web --dry-run             # regenerate with the offline stub provider (no API calls)
```

It reads an existing run's `production.db`, so do a `visualize` (or `visualize
--analyze-only`) first. The page lists every shot; selecting one shows its image,
the **source narration** it was drawn from, and the exact **prompt** sent to the
image model. From there you can:

- **edit the shot** — its visual description, **camera move**, and shot type;
- **split a shot** into two halves and edit each independently;
- **re-render** the frame (optionally with a hand-edited prompt or style override).

All edits and the new frames are persisted to the store, and any continuity notes
from director mode show as a badge in the header. Locally (outside Docker) install
the extra: `pip install -e '.[web]'`.

## Video

Compile rendered frames + the audiobook into timed **mp4s**. Each rendered chunk
becomes its **own segment video** covering only that chunk's time window (its
frames placed at their timestamps, with the matching slice of audio); then a
**master** joins all segments:

```bash
just video --audio book.m4b --dir output/legion       # -> output/legion/<chunk>/video.mp4 (each)
                                                       #  + output/legion/video.mp4 (master)
just video --audio "audiobooks/Moby Dick/" --dir output/moby-dick
```

Each segment's window comes from its chunk folder name (e.g. `0000-60min`), so a
single rendered hour produces a one-hour video — not the frames stretched across
the whole book. Audio may be a single `.m4b` or a folder of `.mp3` parts (each
segment seeks its window out of the shared timeline). A **progress bar** tracks
each encode. Run `visualize` first so frames have timestamps; needs `ffmpeg`.

Each still gets a subtle **Ken Burns** slow zoom for a cinematic feel — disable
with `--no-ken-burns` for static holds (if a filter quirk ever fails, it falls
back to static automatically). The default motion is **crop-safe**: it zooms
*out* and settles on the full, uncropped frame (`--motion out`), anchored
slightly high so heads/faces are never clipped. Tune with `--zoom` (max zoom,
default 1.08; `1.0` = no zoom) and `--motion in|out|alternate`. New frames are
also prompted with headroom so there's room to move. Stills are held for their
full duration (`fps` filter, so the first image no longer flashes), with a gentle
fade in/out per segment (`--fade`, default 0.5s).

In **director mode** each shot instead uses the camera move the director chose
(`push_in`, `pull_out`, `pan_*`, `tilt_*`, `track_*`, `static`); the global Ken
Burns above is the fallback for shots/scenes with no explicit move.

**Incremental re-runs.** Each segment video stores a fingerprint of its inputs
(the frame images plus the render settings) alongside it as `video.fingerprint`.
Re-running `just video` re-renders a segment only when that fingerprint changes —
i.e. you regenerated a frame, added/removed one, or changed an option like
`--fps`/`--zoom`/`--motion`. Untouched segments are reused, and the master is
rebuilt only when some segment actually changed. Pass `--force` to re-render
everything regardless.

**Pacing** is governed by how many scenes the analyzer picks — denser scenes
mean frames that track the narration more closely; tune
`analysis.scenes_per_chunk` / `max_chars_per_chunk` (and re-run with
`--reanalyze` to apply).

For the tightest narration sync, use **per-paragraph mode** — one frame per
paragraph, timed exactly to when it's spoken (no fuzzy alignment, since the
structure carries paragraph timestamps):

```bash
just visualize --structure output/swarm/audiobook_structure_0000-60min.json --per-paragraph
```

Set `analysis.mode: paragraphs` to make it the default, and
`analysis.paragraphs_per_scene` to group N paragraphs per frame (raise it to cut
the frame count / cost). Needs an audiobook structure (the paragraph timestamps).

### Director mode (a crew of role-agents)

Instead of one frame per scene/paragraph, **director mode** runs a small crew of
role-agents that work like a film team: a **screenwriter** breaks the chapter into
proper scenes (slug line, action, beats), a **script critic** reviews and sends
them back for a bounded number of revisions, a **director** breaks each scene into
a **shot list** — shot type, composition, and a **camera move** (`push_in`,
`pull_out`, `pan_*`, `tilt_*`, `track_*`, `static`) — and a **continuity**
supervisor flags inconsistencies (logged to `production.db`). One image is
generated per shot.

```bash
just visualize --structure output/swarm/audiobook_structure_0000-60min.json --director
```

Each shot's exact start/end is pinned in Python (the director only suggests a
relative weight per shot), so the shots **tile each scene's audio window** and the
animatic stays in sync with the narration. When you compile the video, each shot's
camera move drives its **own motion** — `push_in`/`pull_out` zoom, `pan_*`/`track_*`
travel sideways, `tilt_*` vertically, `static` holds — instead of one global Ken
Burns. Scenes, shots and their moves are persisted to `production.db`.

Configure the roster under `crew:` — each role has its own `model`, `max_turns`
and optional `system_prompt`, and `crew.agent_mode` runs each role as an autonomous
multi-turn Claude Code agent (needs the `claude` CLI) rather than a single
structured call. Needs an audiobook structure.

Everything has sane defaults. To tune, copy `config.example.yaml` to
`config.yaml` (auto-discovered) and edit. Highlights:

- `project.style` — the single biggest lever on how frames look.
- `analysis.model` — which Claude model analyzes the text.
- `audio.backend` — `faster-whisper` (local, default) or `openai` (hosted API).
- `generation.provider` — `openai` / `gemini` / `stub` (see above).
- `generation.model` / `size` / `quality` — provider settings.
- `generation.cache` / `character_portraits` / `shot_variety` — consistency & cost.
- `generation.max_frames` / `concurrency` — cost and speed controls.

## Outputs

Each input gets its **own subfolder** so multiple books don't collide:
`output/<book-slug>/` (the slug comes from the input filename). Pass `--out` to
override. When rendering chunks via `--structure`, each chunk gets its own
subfolder under the book dir (see [above](#rendering-chunks-incrementally-no-overwrite-shared-characters)).

Book level (shared across chunks):

- `production.db` — the source of truth: characters, scenes/shots, and frame
  records (prompt, provider, references, resulting file) for every chunk.
- `portraits/<name>_<hash>.png` — reference portraits, cached by appearance.
- `audiobook_structure.json` — chapters + paragraphs with audio timestamps.

Per run / per chunk:

- `frames/*.png` — one image per shot (widescreen).
- `gallery.html` — a self-contained gallery to browse the frames.
- `video.mp4` — the compiled segment video (after `just video`).

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
4. **Generate** (`generation/`) — render a reference portrait per character, then
   build a prompt per scene (shot type + canonical character appearances) and
   render it with the configured provider, passing portraits as references where
   supported. Content-addressed caching skips unchanged images.
5. **Gallery** (`gallery.py`) — assemble frames + metadata into static HTML.
6. **Web UI** (`web/`) — a FastAPI app to browse frames and regenerate any one
   of them on demand (edited prompt / style), reusing the pipeline and cache.

## Extending

- **New image backend** (Replicate, local Stable Diffusion, ...): implement
  `ImageProvider.generate` in `generation/`, set `supports_references` if it can
  take reference images, and register it in `generation/factory.py`. Nothing
  else changes — see `gemini_provider.py` for a reference-capable example.
- **Better prompts**: edit `analysis/prompts.py` (analysis) and
  `generation/prompt_builder.py` (image prompt assembly).

## Roadmap

Done:

- ✅ Google Gemini 2.5 Flash Image ("Nano Banana") provider.
- ✅ Reference-image / character-portrait conditioning for stronger consistency.
- ✅ Shot-type variety (wide/medium/close-up).
- ✅ Caching so re-runs only regenerate changed scenes.
- ✅ Web UI for browsing and regenerating individual frames.

Still ahead:

- True forced alignment (e.g. WhisperX) instead of fuzzy excerpt matching.
- Per-chapter pacing controls (frame density per chapter).

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
