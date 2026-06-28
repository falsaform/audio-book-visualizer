"""Command-line interface.

    abv visualize --ebook book.epub --audio book.m4b
    abv visualize --ebook book.pdf --analyze-only
    abv visualize --ebook book.txt --dry-run        # no API calls, stub frames

Run ``abv --help`` for all options.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .config import Config, load_env
from .pipeline import Pipeline
from .utils import slugify

app = typer.Typer(
    add_completion=False,
    help="Turn an audiobook + ebook into AI-analyzed scenes and generated still frames.",
)
console = Console()


def _progress(msg: str) -> None:
    console.print(f"[dim]·[/dim] {msg}")


def _resolve_output_dir(
    config: Config,
    out: Optional[Path],
    *,
    ebook: Optional[Path] = None,
    audio: Optional[Path] = None,
    structure: Optional[Path] = None,
) -> str:
    """Pick the output dir: an explicit --out wins, else a per-book subfolder.

    Subfolder name derives from the input file so multiple books don't collide:
    ``output/<book-slug>/``. When reusing a structure, output lands next to it.
    """
    if out is not None:
        return str(out)
    if structure is not None:
        return str(Path(structure).parent)
    base = Path(config.output.dir)
    stem = None
    if ebook is not None:
        stem = ebook.stem
    elif audio is not None:
        stem = audio.stem
    return str(base / slugify(stem)) if stem else str(base)


@app.command()
def visualize(
    ebook: Optional[Path] = typer.Option(
        None, "--ebook", "-e", help="Path to .pdf/.epub/.txt ebook."
    ),
    audio: Optional[Path] = typer.Option(
        None, "--audio", "-a", help="Audiobook file, or a folder of audio parts."
    ),
    structure: Optional[Path] = typer.Option(
        None,
        "--structure",
        help="Reuse an existing audiobook_structure.json (skip transcription).",
    ),
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="Path to config.yaml (defaults to ./config.yaml)."
    ),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", help="Output directory (overrides config)."
    ),
    style: Optional[str] = typer.Option(
        None, "--style", "-s", help="Override the visual style for frames."
    ),
    max_frames: Optional[int] = typer.Option(
        None, "--max-frames", "-n", help="Cap the number of frames generated."
    ),
    provider: Optional[str] = typer.Option(
        None, "--provider", "-p", help="Image provider: openai | gemini | stub."
    ),
    chapter_mode: Optional[str] = typer.Option(
        None,
        "--chapter-mode",
        help="Audiobook-only chapter detection: auto|markers|headings|time|single.",
    ),
    analyze_only: bool = typer.Option(
        False, "--analyze-only", help="Run analysis and stop before image generation."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Use the offline stub image provider (no API calls)."
    ),
    force: bool = typer.Option(
        False, "--force", help="Regenerate all frames (and portraits), even if they exist."
    ),
    reanalyze: bool = typer.Option(
        False, "--reanalyze", help="Re-run analysis even if analysis.json already exists."
    ),
):
    """Run the full pipeline: ingest -> analyze -> generate frames -> gallery.

    If a chunk's analysis.json already exists it is reused (skip re-analysis);
    pass --reanalyze to regenerate it. Frames/portraits are kept if their file
    exists — delete the ones you want redone (or use --force) and re-run.
    """
    load_env()
    config = Config.load(config_path)

    # Apply CLI overrides.
    config.output.dir = _resolve_output_dir(
        config, out, ebook=ebook, audio=audio, structure=structure
    )
    if style is not None:
        config.project.style = style
    if max_frames is not None:
        config.generation.max_frames = max_frames
    if provider is not None:
        config.generation.provider = provider
    if chapter_mode is not None:
        config.audio.chapter_mode = chapter_mode

    if not ebook and not audio and not structure:
        console.print("[red]Error:[/red] provide --ebook, --audio, and/or --structure.")
        raise typer.Exit(code=2)

    pipeline = Pipeline(config, on_progress=_progress)
    try:
        analysis = pipeline.run(
            ebook_path=str(ebook) if ebook else None,
            audio_path=str(audio) if audio else None,
            structure_path=str(structure) if structure else None,
            analyze_only=analyze_only,
            dry_run=dry_run,
            force=force,
            reanalyze=reanalyze,
        )
    except Exception as exc:  # noqa: BLE001 - surface a clean message to the user
        console.print(f"[red]Pipeline failed:[/red] {exc}")
        raise typer.Exit(code=1)

    _summary(analysis, config.output.dir)


@app.command()
def web(
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", help="Output directory to browse (defaults to config)."
    ),
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="Path to config.yaml."
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host (use 0.0.0.0 in Docker)."),
    port: int = typer.Option(8000, "--port", help="Bind port."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Regenerate with the offline stub provider (no API calls)."
    ),
):
    """Serve a web UI to browse frames and regenerate them individually."""
    load_env()
    config = Config.load(config_path)
    if out is not None:
        config.output.dir = str(out)

    try:
        import uvicorn

        from .web.server import create_app
    except ImportError:
        console.print(
            "[red]Web extras not installed.[/red] Install with "
            "`pip install -e '.[web]'` (or use the Docker image / `just web`)."
        )
        raise typer.Exit(code=1)

    application = create_app(config.output.dir, config, dry_run=dry_run)
    console.print(
        f"[green]Serving[/green] {config.output.dir} at "
        f"[underline]http://{host}:{port}[/underline]  (Ctrl-C to stop)"
    )
    uvicorn.run(application, host=host, port=port, log_level="warning")


@app.command()
def segment(
    audio: Path = typer.Option(
        ..., "--audio", "-a", help="Audiobook file, or a folder of audio parts."
    ),
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="Path to config.yaml."
    ),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", help="Output directory (overrides config)."
    ),
    chapter_mode: Optional[str] = typer.Option(
        None, "--chapter-mode", help="auto|markers|headings|time|single."
    ),
    start: float = typer.Option(
        0.0, "--start", help="Preview start offset, in MINUTES (default 0)."
    ),
    duration: Optional[float] = typer.Option(
        None,
        "--duration",
        "-d",
        help="Preview length, in MINUTES. Omit to process to the end. "
        "Use a small value (e.g. -d 10) to iterate fast on large files.",
    ),
):
    """Transcribe an audiobook and segment it into chapters + paragraphs.

    Audiobook-only and API-free: writes audiobook_structure.json with each
    chapter and paragraph plus audio timestamps, so pieces can be processed
    individually. No analysis or image generation.

    Use --start/--duration (in minutes) to preview just a slice of a long file
    and iterate quickly, e.g. `abv segment -a book.m4b --start 60 -d 10`.
    """
    from .ingest import build_audiobook_structure

    load_env()
    config = Config.load(config_path)
    config.output.dir = _resolve_output_dir(config, out, audio=audio)
    if chapter_mode is not None:
        config.audio.chapter_mode = chapter_mode

    start_s = max(0.0, start) * 60.0
    duration_s = duration * 60.0 if duration is not None else None

    try:
        where = config.audio.backend
        if start_s or duration_s is not None:
            end_label = f"{start + (duration or 0):g}" if duration is not None else "end"
            _progress(f"Preview window: minutes {start:g}–{end_label}")
        _progress(f"Transcribing audio ({where}): {audio}")
        transcript = _transcribe_with_progress(audio, config, start_s, duration_s)
        _progress(f"  {len(transcript.segments)} transcript segment(s)")
        _progress("Segmenting into chapters and paragraphs")
        from .ingest import audio_file_boundaries

        boundaries = audio_file_boundaries(str(audio))
        structure = build_audiobook_structure(
            transcript, str(audio), config.audio, title=audio.stem,
            file_boundaries=boundaries,
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Segmentation failed:[/red] {exc}")
        raise typer.Exit(code=1)

    out_dir = Path(config.output.dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Windowed previews get a distinct name so sub-segments don't overwrite each
    # other (and can be joined later with `abv join`).
    if start_s or duration_s is not None:
        end_min = int(start + duration) if duration is not None else "end"
        fname = f"audiobook_structure_{int(start):04d}-{end_min}min.json"
    else:
        fname = "audiobook_structure.json"
    structure_path = out_dir / fname
    structure_path.write_text(structure.model_dump_json(indent=2), encoding="utf-8")

    table = Table(title=f"Audiobook structure — {structure.title} (via {structure.source})")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Chapter", style="bold cyan")
    table.add_column("Paras", justify="right")
    table.add_column("Span")
    for ch in structure.chapters:
        span = f"{_fmt_time(ch.start)}–{_fmt_time(ch.end)}"
        table.add_row(str(ch.index + 1), ch.title, str(len(ch.paragraphs)), span)
    console.print(table)
    console.print(f"  Wrote: [underline]{structure_path}[/underline]")


def _fmt_time(seconds: Optional[float]) -> str:
    if seconds is None:
        return "?"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


@app.command()
def join(
    structures: list[Path] = typer.Argument(
        ..., help="Sub-segment audiobook_structure_*.json files to join."
    ),
    out: Path = typer.Option(
        ..., "--out", "-o", help="Combined audiobook_structure.json to write."
    ),
    title: Optional[str] = typer.Option(None, "--title", help="Title for the combined structure."),
):
    """Join sub-segment structure files into one (ordered by timestamp).

    Segment a long book in slices (`abv segment -a book.m4b --start S -d D`),
    then join the pieces here. The result feeds `abv visualize --structure`.
    """
    from .ingest import join_structures
    from .models import AudiobookStructure

    loaded: list[AudiobookStructure] = []
    for path in structures:
        try:
            loaded.append(AudiobookStructure.model_validate_json(Path(path).read_text()))
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Could not read {path}:[/red] {exc}")
            raise typer.Exit(code=1)

    combined = join_structures(loaded, title=title)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(combined.model_dump_json(indent=2), encoding="utf-8")

    n_para = sum(len(c.paragraphs) for c in combined.chapters)
    table = Table(title=f"Joined {len(loaded)} segment(s) — {combined.title}")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Chapter", style="bold cyan")
    table.add_column("Paras", justify="right")
    table.add_column("Span")
    for ch in combined.chapters:
        table.add_row(
            str(ch.index + 1), ch.title, str(len(ch.paragraphs)),
            f"{_fmt_time(ch.start)}–{_fmt_time(ch.end)}",
        )
    console.print(table)
    console.print(
        f"  {len(combined.chapters)} chapter(s), {n_para} paragraph(s) -> "
        f"[underline]{out}[/underline]"
    )


@app.command()
def video(
    audio: Path = typer.Option(
        ..., "--audio", "-a", help="Audiobook file or folder (the audio track)."
    ),
    dir: Optional[Path] = typer.Option(
        None, "--dir", "-d", help="Book output dir with rendered frames (default: config output)."
    ),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", help="Output mp4 path (default: <dir>/video.mp4)."
    ),
    config_path: Optional[Path] = typer.Option(None, "--config", "-c", help="Path to config.yaml."),
    fps: int = typer.Option(24, "--fps", help="Output frame rate."),
):
    """Compile generated frames + audio into a timed video (mp4).

    Each frame is shown during its scene's time span, synced to the audio (one
    m4b or a folder of mp3s). Run `visualize` first so frames have timestamps.
    """
    from .video import compile_video

    load_env()
    config = Config.load(config_path)
    book_dir = dir if dir is not None else Path(config.output.dir)
    out_path = out if out is not None else (book_dir / "video.mp4")

    try:
        result = compile_video(book_dir, str(audio), out_path, fps=fps, on_progress=_progress)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Video compilation failed:[/red] {exc}")
        raise typer.Exit(code=1)

    console.print(f"[green]✓[/green] Wrote video -> [underline]{result}[/underline]")


def _transcribe_with_progress(
    audio: Path, config: Config, start: float = 0.0, duration: Optional[float] = None
):
    """Transcribe with a live progress bar tracking audio position.

    The bar advances as transcribed audio time approaches the file's duration,
    so a long audiobook shows steady movement instead of a silent wait.
    """
    from rich.progress import (
        BarColumn,
        Progress,
        TaskProgressColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )

    from .ingest import transcribe_audio

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("elapsed •"),
        TimeRemainingColumn(),
        TextColumn("left"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Transcribing", total=None)

        def on_progress(done: float, total: float) -> None:
            # total becomes known once decoding starts; set it then.
            progress.update(task, total=total or None, completed=done)

        return transcribe_audio(
            str(audio),
            backend=config.audio.backend,
            model=config.audio.model,
            on_progress=on_progress,
            chunk_seconds=config.audio.chunk_seconds,
            start=start,
            duration=duration,
        )


@app.command()
def characters(
    analysis_json: Path = typer.Argument(..., help="Path to a generated analysis.json."),
):
    """Print the character bible from a previous analysis."""
    from .models import BookAnalysis

    data = json.loads(Path(analysis_json).read_text())
    analysis = BookAnalysis.model_validate(data)

    table = Table(title=f"Characters — {analysis.title or 'Untitled'}")
    table.add_column("Name", style="bold cyan")
    table.add_column("Role")
    table.add_column("Appearance")
    for char in analysis.characters:
        name = char.name + (f"\n[dim]({', '.join(char.aliases)})[/dim]" if char.aliases else "")
        table.add_row(name, char.role, char.description)
    console.print(table)


def _summary(analysis, out_dir: str) -> None:
    console.print()
    console.print(
        f"[green]✓[/green] [bold]{analysis.title or 'Analysis complete'}[/bold]"
    )
    console.print(
        f"  {len(analysis.characters)} characters · {len(analysis.scenes)} scenes"
    )

    # Report frame outcomes from the manifest, if generation ran.
    manifest = Path(out_dir) / "manifest.json"
    if manifest.exists():
        try:
            frames = json.loads(manifest.read_text())
        except (json.JSONDecodeError, OSError):
            frames = []
        ok = [f for f in frames if f.get("image_path") and not f.get("error")]
        failed = [f for f in frames if f.get("error")]
        console.print(f"  {len(ok)}/{len(frames)} frames generated")
        if failed:
            console.print(f"  [yellow]⚠ {len(failed)} frame(s) failed.[/yellow]")
            first = failed[0]["error"].splitlines()[0][:200]
            console.print(f"    [dim]{first}[/dim]")

    console.print(f"  Output: [underline]{out_dir}[/underline]")
    gallery = Path(out_dir) / "gallery.html"
    if gallery.exists():
        console.print(f"  Open: [underline]{gallery}[/underline]")


if __name__ == "__main__":
    app()
