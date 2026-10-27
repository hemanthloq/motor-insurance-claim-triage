"""Corpus loading (D2).

Two source types are supported: PDF with a text layer, and HTML. Scanned PDFs
are detected and rejected with a clear message rather than silently indexed as
empty -- a silently empty document is the worst possible failure for a system
whose job is to cite text.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..logging_setup import get_logger

log = get_logger("loader")

#: Below this many extracted characters per page we assume there is no text
#: layer and the document needs OCR before it can be indexed.
MIN_CHARS_PER_PAGE = 120


class NoTextLayer(RuntimeError):
    """Raised when a PDF yields too little text to be indexable."""


@dataclass
class LoadedPage:
    number: int
    text: str


@dataclass
class LoadedDocument:
    document_id: str
    title: str
    path: str
    source_url: str
    kind: str
    pages: list[LoadedPage] = field(default_factory=list)
    content_sha256: str = ""
    retrieved_at: str = ""

    @property
    def text(self) -> str:
        return "\n".join(page.text for page in self.pages)

    @property
    def page_count(self) -> int:
        return len(self.pages)


_LIGATURES = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", " ": " ", "•": "* ",
}


#: A line that opens a new structural element -- never joined onto the line above.
_STRUCTURE_START = re.compile(
    r"^(?:CHAPTER\b|SECTION\b|\(?[0-9]{1,2}[.)]\s|\(?[ivxIVX]{1,5}[.)]\s|\(?[a-z][.)]\s|[*\-•]\s)",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"[.;:?!]$")
_ALL_CAPS = re.compile(r"^[^a-z]*[A-Z][^a-z]*$")


def _is_all_caps(line: str) -> bool:
    return bool(_ALL_CAPS.match(line)) and any(c.isalpha() for c in line)


def _should_join(previous: str, current: str) -> bool:
    """Is `current` the continuation of a line wrapped by the typesetter?

    This is the single highest-leverage piece of text handling in the project.
    Policy wordings are set in narrow columns, so the extractor emits a hard
    newline mid-sentence. Join too little and "hire or reward" is split across
    lines and the lexical index stops matching the phrase that the whole
    section 4.1 case turns on. Join too much and `CHAPTER 4 - Limitations as to
    Use` is absorbed into the paragraph above it, the chunker never sees a
    heading, and every clause in that chapter is filed under the wrong section
    path -- which is exactly what happened on the first pass, because the PDF
    leaves a trailing space after the preceding sentence and a naive
    "previous character was a full stop" test misses it.
    """
    if _SENTENCE_END.search(previous):
        return False
    if _STRUCTURE_START.match(current):
        return False
    if re.match(r"^(?:CHAPTER|SECTION)\b", previous, re.IGNORECASE):
        # A chapter heading rarely ends in a full stop, so the sentence test
        # above will not catch it. Absorbing the first line of the chapter into
        # its own heading is how `CHAPTER 4 - Limitations as to Use` turns into
        # a 70-character section path with a sentence stuck on the end.
        return False
    previous_caps, current_caps = _is_all_caps(previous), _is_all_caps(current)
    if previous_caps != current_caps:
        # a heading followed by its body, or a body followed by a heading
        return False
    return True


def normalise(text: str) -> str:
    """Repair the usual PDF extraction damage without losing the structure."""
    for bad, good in _LIGATURES.items():
        text = text.replace(bad, good)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # de-hyphenate across a line break: "exclu-\nsion" -> "exclusion"
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)

    out: list[str] = []
    for raw_line in text.split("\n"):
        line = re.sub(r"[ \t]{2,}", " ", raw_line).strip()
        if not line:
            if out and out[-1] != "":
                out.append("")
            continue
        if out and out[-1] and _should_join(out[-1], line):
            out[-1] = f"{out[-1]} {line}"
        else:
            out.append(line)
    return "\n".join(out).strip()


#: A line repeated on at least this share of pages is page furniture, not text.
RUNNING_LINE_SHARE = 0.30


def strip_running_lines(pages: list[LoadedPage]) -> list[LoadedPage]:
    """Remove running headers, footers and page numbers.

    `PRIVATE CAR PACKAGE POLICY WORDING` sits at the top of all 46 pages of the
    wording. Left in, the chunker reads it as an all-caps heading once per page
    and every clause in the document is filed under it. Removing it is worth
    more to retrieval quality than any amount of tuning further downstream.
    """
    if len(pages) < 4:
        return pages
    counts: dict[str, int] = {}
    for page in pages:
        for line in {l.strip() for l in page.text.split("\n") if l.strip()}:
            if len(line) <= 100:
                counts[line] = counts.get(line, 0) + 1
    threshold = max(3, int(len(pages) * RUNNING_LINE_SHARE))
    furniture = {line for line, count in counts.items() if count >= threshold}
    page_number_re = re.compile(r"^(?:page\s+)?\d{1,3}(?:\s*/\s*\d{1,3})?$", re.IGNORECASE)
    if not furniture:
        return pages
    cleaned = []
    for page in pages:
        kept = [
            line
            for line in page.text.split("\n")
            if line.strip() not in furniture and not page_number_re.match(line.strip())
        ]
        cleaned.append(LoadedPage(number=page.number, text="\n".join(kept).strip()))
    return cleaned


def load_pdf(path: Path, *, document_id: str, title: str, source_url: str) -> LoadedDocument:
    import pypdf

    reader = pypdf.PdfReader(str(path))
    pages: list[LoadedPage] = []
    for number, page in enumerate(reader.pages, start=1):
        raw = page.extract_text() or ""
        pages.append(LoadedPage(number=number, text=normalise(raw)))
    pages = strip_running_lines(pages)

    total = sum(len(p.text) for p in pages)
    if pages and total / max(len(pages), 1) < MIN_CHARS_PER_PAGE:
        raise NoTextLayer(
            f"{path.name}: {total} characters over {len(pages)} pages. "
            "This looks like a scanned document; run OCR before indexing it."
        )

    return LoadedDocument(
        document_id=document_id,
        title=title,
        path=str(path),
        source_url=source_url,
        kind="pdf",
        pages=pages,
        content_sha256=sha256_file(path),
    )


def load_html(path: Path, *, document_id: str, title: str, source_url: str) -> LoadedDocument:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="ignore"), "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()
    text = normalise(soup.get_text("\n"))
    return LoadedDocument(
        document_id=document_id,
        title=title,
        path=str(path),
        source_url=source_url,
        kind="html",
        pages=[LoadedPage(number=1, text=text)],
        content_sha256=sha256_file(path),
    )


def load_text(path: Path, *, document_id: str, title: str, source_url: str) -> LoadedDocument:
    text = normalise(path.read_text(encoding="utf-8", errors="ignore"))
    return LoadedDocument(
        document_id=document_id,
        title=title,
        path=str(path),
        source_url=source_url,
        kind="text",
        pages=[LoadedPage(number=1, text=text)],
        content_sha256=sha256_file(path),
    )


def load(path: Path, *, document_id: str, title: str, source_url: str = "") -> LoadedDocument:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return load_pdf(path, document_id=document_id, title=title, source_url=source_url)
    if suffix in {".html", ".htm"}:
        return load_html(path, document_id=document_id, title=title, source_url=source_url)
    return load_text(path, document_id=document_id, title=title, source_url=source_url)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()
