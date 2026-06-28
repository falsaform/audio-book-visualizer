# Project recipes. Everything runs in Docker via docker compose.
# Run `just` (or `just --list`) to see all recipes.

compose := "docker compose"
service := "app"
run := compose + " run --rm " + service
# Non-interactive variant (no TTY) for CI and scripted use.
runci := compose + " run --rm -T " + service

# Show available recipes
default:
    @just --list

# --- lifecycle -------------------------------------------------------------

# Build the docker image
build:
    {{compose}} build

# Rebuild from scratch (no cache)
rebuild:
    {{compose}} build --no-cache

# Resolve/update the uv lockfile, then rebuild the image
lock:
    {{run}} uv lock
    {{compose}} build

# Open an interactive shell in the container
shell:
    {{run}} bash

# Run the Claude Code CLI in the container (auth via CLAUDE_CODE_OAUTH_TOKEN)
# e.g. `just claude` for a session, or `just claude -p "explain src/..."`
claude *args:
    {{run}} claude {{args}}

# Stop and remove containers + volumes
down:
    {{compose}} down -v

# Remove generated outputs (host side)
clean:
    rm -rf output

# --- run the pipeline ------------------------------------------------------

# Run the abv CLI with arbitrary args, e.g. `just abv characters output/analysis.json`
abv *args:
    {{run}} abv {{args}}

# Run the full visualizer, e.g. `just visualize --ebook book.epub --audio book.m4b`
visualize *args:
    {{run}} abv visualize {{args}}

# Audiobook-only: transcribe + segment into chapters/paragraphs (no API keys)
# e.g. `just segment --audio book.m4b`
segment *args:
    {{run}} abv segment {{args}}

# Offline demo on the bundled sample (stub frames, no image API; needs ANTHROPIC_API_KEY)
demo:
    {{run}} abv visualize --ebook examples/sample.txt --dry-run --max-frames 4

# Serve the web UI (browse + regenerate frames) at http://localhost:8000
# Pass extra args, e.g. `just web --dry-run` or `just web --out runs/moby`
web *args:
    {{compose}} run --rm --service-ports {{service}} abv web --host 0.0.0.0 {{args}}

# --- quality ---------------------------------------------------------------

# Run the test suite (pass extra args, e.g. `just test -k align`)
test *args:
    {{runci}} pytest {{args}}

# Lint with ruff
lint:
    {{runci}} ruff check .

# Format with ruff
fmt:
    {{run}} ruff format .
