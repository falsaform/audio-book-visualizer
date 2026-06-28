"""Command-line interface.

    abv visualize --ebook book.epub --audio book.m4b
    abv visualize --ebook book.pdf --analyze-only
    abv visualize --ebook book.txt --dry-run        # no API calls, stub frames

Run ``abv --help`` for all options.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .config import Config, load_env
from .pipeline import Pipeline

app = typer.Typer(
    add_completion=False,
    help="Turn an audiobook + ebook into AI-analyzed scenes and generated still frames.",
)
console = Console()


def _progress(msg: str) -> None:
    console.print(f"[dim]·[/dim] {msg}")


@app.command()
def visualize(
    ebook: Optional[Path] = typer.Option(
        None, "--ebook", "-e", help="Path to .pdf/.epub/.txt ebook."
    ),
    audio: Optional[Path] = typer.Option(
        None, "--audio", "-a", help="Path to audiobook (.mp3/.m4a/.m4b/.wav)."
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
        False, "--force", help="Regenerate all frames, ignoring the cache."
    ),
):
    """Run the full pipeline: ingest -> analyze -> generate frames -> gallery."""
    load_env()
    config = Config.load(config_path)

    # Apply CLI overrides.
    if out is not None:
        config.output.dir = str(out)
    if style is not None:
        config.project.style = style
    if max_frames is not None:
        config.generation.max_frames = max_frames
    if provider is not None:
        config.generation.provider = provider
    if chapter_mode is not None:
        config.audio.chapter_mode = chapter_mode

    if not ebook and not audio:
        console.print("[red]Error:[/red] provide --ebook and/or --audio.")
        raise typer.Exit(code=2)

    pipeline = Pipeline(config, on_progress=_progress)
    try:
        analysis = pipeline.run(
            ebook_path=str(ebook) if ebook else None,
            audio_path=str(audio) if audio else None,
            analyze_only=analyze_only,
            dry_run=dry_run,
            force=force,
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
    audio: Path = typer.Option(..., "--audio", "-a", help="Path to the audiobook file."),
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="Path to config.yaml."
    ),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", help="Output directory (overrides config)."
    ),
    chapter_mode: Optional[str] = typer.Option(
        None, "--chapter-mode", help="auto|markers|headings|time|single."
    ),
):
    """Transcribe an audiobook and segment it into chapters + paragraphs.

    Audiobook-only and API-free: writes audiobook_structure.json with each
    chapter and paragraph plus audio timestamps, so pieces can be processed
    individually. No analysis or image generation.
    """
    from .ingest import build_audiobook_structure

    load_env()
    config = Config.load(config_path)
    if out is not None:
        config.output.dir = str(out)
    if chapter_mode is not None:
        config.audio.chapter_mode = chapter_mode

    try:
        _progress(f"Transcribing audio ({config.audio.backend}): {audio}")
        transcript = _transcribe_with_progress(audio, config)
        _progress(f"  {len(transcript.segments)} transcript segment(s)")
        _progress("Segmenting into chapters and paragraphs")
        structure = build_audiobook_structure(
            transcript, str(audio), config.audio, title=audio.stem
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Segmentation failed:[/red] {exc}")
        raise typer.Exit(code=1)

    out_dir = Path(config.output.dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    structure_path = out_dir / "audiobook_structure.json"
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


def _transcribe_with_progress(audio: Path, config: Config):
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
        )


@app.command()
def characters(
    analysis_json: Path = typer.Argument(..., help="Path to a generated analysis.json."),
):
    """Print the character bible from a previous analysis."""
    import json

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
    console.print(f"  Output: [underline]{out_dir}[/underline]")
    gallery = Path(out_dir) / "gallery.html"
    if gallery.exists():
        console.print(f"  Open: [underline]{gallery}[/underline]")


if __name__ == "__main__":
    app()
