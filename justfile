# Project recipes. Everything runs in Docker via docker compose.
# Run `just` (or `just --list`) to see all recipes.

compose := "docker compose"
service := "app"
# Run as the host user/group so files written to ./output (and the rest of the
# bind-mounted workspace) are owned by you, not root. Falls back to root on hosts
# without `id` (e.g. native Windows — use WSL).
uid := `id -u 2>/dev/null || echo 0`
gid := `id -g 2>/dev/null || echo 0`
asuser := "--user " + uid + ":" + gid
run := compose + " run --rm " + asuser + " " + service
# Non-interactive variant (no TTY) for CI and scripted use.
runci := compose + " run --rm -T " + asuser + " " + service

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

# Run the full visualizer, e.g. `just visualize --audio book.m4b --director`
visualize *args:
    {{run}} abv visualize {{args}}

# Audiobook-only: transcribe + segment into chapters/paragraphs (no API keys)
# e.g. `just segment --audio book.m4b`
segment *args:
    {{run}} abv segment {{args}}

# Compile rendered frames + audio into a timed mp4
# e.g. `just video --audio book.m4b --dir output/legion`
video *args:
    {{run}} abv video {{args}}

# Director-mode demo on the bundled structure sample (stub frames, no image API;
# needs ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN for the analysis crew)
demo:
    {{run}} abv visualize --structure examples/sample_structure.json --director --dry-run --out output/sample

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
