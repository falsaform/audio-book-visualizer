"""Ebook ingestion for PDF, EPUB and plain-text sources.

Returns cleaned, chapter-aware text. Chapter detection is best-effort: EPUB has
real document boundaries; PDF and TXT fall back to heuristic splitting so the
rest of the pipeline always has *some* structure to work with.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Chapter:
    title: str
    text: str


@dataclass
class Ebook:
    title: str = ""
    author: str = ""
    chapters: list[Chapter] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        return "\n\n".join(ch.text for ch in self.chapters)


def load_ebook(path: str | Path) -> Ebook:
    """Load an ebook from a ``.pdf``, ``.epub`` or ``.txt`` file."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _load_pdf(path)
    if suffix == ".epub":
        return _load_epub(path)
    if suffix in (".txt", ".text", ".md"):
        return _load_text(path)
    raise ValueError(f"Unsupported ebook format: {suffix!r} (use .pdf/.epub/.txt)")


def _clean(text: str) -> str:
    # Collapse hyphenated line breaks, then normalize whitespace.
    text = re.sub(r"-\n(\w)", r"\1", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _load_text(path: Path) -> Ebook:
    raw = path.read_text(encoding="utf-8", errors="replace")
    chapters = _split_into_chapters(_clean(raw))
    return Ebook(title=path.stem, chapters=chapters)


def _load_pdf(path: Path) -> Ebook:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    meta = reader.metadata or {}
    pages = [page.extract_text() or "" for page in reader.pages]
    text = _clean("\n".join(pages))
    chapters = _split_into_chapters(text)
    return Ebook(
        title=(meta.title or path.stem) if hasattr(meta, "title") else path.stem,
        author=getattr(meta, "author", "") or "",
        chapters=chapters,
    )


def _load_epub(path: Path) -> Ebook:
    from bs4 import BeautifulSoup
    from ebooklib import epub
    import ebooklib

    book = epub.read_epub(str(path))
    title = ""
    author = ""
    if book.get_metadata("DC", "title"):
        title = book.get_metadata("DC", "title")[0][0]
    if book.get_metadata("DC", "creator"):
        author = book.get_metadata("DC", "creator")[0][0]

    chapters: list[Chapter] = []
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        soup = BeautifulSoup(item.get_content(), "html.parser")
        text = _clean(soup.get_text(separator="\n"))
        if len(text) < 200:  # skip covers, copyright pages, tiny fragments
            continue
        heading = soup.find(["h1", "h2", "h3"])
        chap_title = heading.get_text(strip=True) if heading else f"Section {len(chapters) + 1}"
        chapters.append(Chapter(title=chap_title, text=text))

    if not chapters:
        chapters = [Chapter(title="Full text", text="(no readable content)")]
    return Ebook(title=title or path.stem, author=author, chapters=chapters)


_CHAPTER_RE = re.compile(
    r"^\s*(chapter\s+[\divxlcm]+|chapter\s+\w+|part\s+[\divxlcm]+|prologue|epilogue)\b.*$",
    re.IGNORECASE | re.MULTILINE,
)


def _split_into_chapters(text: str) -> list[Chapter]:
    """Heuristically split flat text on 'Chapter N' style headings."""
    matches = list(_CHAPTER_RE.finditer(text))
    if not matches:
        return [Chapter(title="Full text", text=text)]

    chapters: list[Chapter] = []
    preamble = text[: matches[0].start()].strip()
    if len(preamble) > 200:
        chapters.append(Chapter(title="Prologue", text=preamble))

    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        title = match.group(0).strip()
        body = text[start:end].strip()
        chapters.append(Chapter(title=title, text=body))
    return chapters
