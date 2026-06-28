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
    analyze_only: bool = typer.Option(
        False, "--analyze-only", help="Run analysis and stop before image generation."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Use the offline stub image provider (no API calls)."
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
        )
    except Exception as exc:  # noqa: BLE001 - surface a clean message to the user
        console.print(f"[red]Pipeline failed:[/red] {exc}")
        raise typer.Exit(code=1)

    _summary(analysis, config.output.dir)


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
