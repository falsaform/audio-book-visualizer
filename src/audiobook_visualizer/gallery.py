"""Render a browsable HTML gallery of generated frames (from the production store)."""

from __future__ import annotations

import html
from pathlib import Path

from .store import ProductionView


def _fmt_time(seconds: float | None) -> str:
    if seconds is None:
        return ""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def render_gallery(view: ProductionView | None, out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    shots = view.shots if view else []

    cards = []
    ok_count = 0
    last_heading = None
    for shot in shots:
        frame = shot.frame
        title = html.escape(shot.title or shot.slug)
        summary = html.escape(shot.summary or shot.visual_description)
        chapter = html.escape(shot.chapter or "")
        ts = _fmt_time(shot.start_time)
        meta_bits = " · ".join(b for b in [chapter, ts] if b)

        # In director mode, group the shots under their scene's slug line.
        heading = shot.heading or ""
        if heading and heading != last_heading:
            cards.append(f'<h2 class="section">{html.escape(heading)}</h2>')
            last_heading = heading

        badges = " ".join(
            f'<span class="badge">{html.escape(b)}</span>'
            for b in [shot.shot_type, shot.camera_move] if b
        )

        if frame and frame.ok and frame.image_path:
            ok_count += 1
            rel = Path(frame.image_path)
            try:
                rel = rel.relative_to(out_dir)
            except ValueError:
                rel = Path(frame.image_path).name
            media = f'<img loading="lazy" src="{html.escape(str(rel))}" alt="{title}">'
        else:
            err = html.escape((frame.error if frame else None) or "not generated")
            media = f'<div class="missing">⚠ {err}</div>'

        cards.append(
            f"""
        <figure class="card">
          {media}
          <figcaption>
            <h3>{title}</h3>
            <p class="meta">{meta_bits}</p>
            {f'<p class="badges">{badges}</p>' if badges else ''}
            <p class="summary">{summary}</p>
          </figcaption>
        </figure>"""
        )

    book_title = html.escape((view.title if view else "") or "Audiobook Visualizer")
    author = html.escape(view.author if view else "")
    doc = _TEMPLATE.format(
        title=book_title,
        author=f" — {author}" if author else "",
        count=ok_count,
        total=len(shots),
        cards="\n".join(cards),
    )
    out_path = out_dir / "gallery.html"
    out_path.write_text(doc, encoding="utf-8")
    return out_path


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Visualized</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin: 0; font-family: system-ui, sans-serif; background:#111; color:#eee; }}
  header {{ padding: 2rem; border-bottom: 1px solid #333; }}
  header h1 {{ margin: 0 0 .25rem; font-size: 1.6rem; }}
  header p {{ margin: 0; color:#999; }}
  .grid {{ display:grid; gap:1.25rem; padding:1.5rem;
           grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); }}
  .card {{ margin:0; background:#1a1a1a; border-radius:10px; overflow:hidden;
           box-shadow:0 2px 10px rgba(0,0,0,.4); }}
  .card img {{ width:100%; display:block; aspect-ratio:16/9; object-fit:cover; }}
  .missing {{ aspect-ratio:16/9; display:flex; align-items:center; justify-content:center;
              background:#2a1a1a; color:#e88; padding:1rem; text-align:center; }}
  figcaption {{ padding: .85rem 1rem 1.1rem; }}
  figcaption h3 {{ margin:0 0 .3rem; font-size:1.05rem; }}
  .meta {{ margin:0 0 .5rem; color:#7aa; font-size:.8rem; }}
  .badges {{ margin:0 0 .5rem; display:flex; flex-wrap:wrap; gap:.3rem; }}
  .badge {{ background:#233; color:#9cd; padding:.1rem .45rem; border-radius:6px;
            font-size:.72rem; text-transform:uppercase; letter-spacing:.03em; }}
  .summary {{ margin:0; color:#bbb; font-size:.9rem; line-height:1.4; }}
  .section {{ grid-column: 1 / -1; margin:.5rem 0 -.25rem; padding-bottom:.4rem;
              border-bottom:1px solid #333; color:#cdd; font-size:1rem;
              font-family: ui-monospace, monospace; letter-spacing:.02em; }}
</style>
</head>
<body>
<header>
  <h1>{title}{author}</h1>
  <p>{count} of {total} frames generated</p>
</header>
<main class="grid">
{cards}
</main>
</body>
</html>
"""
