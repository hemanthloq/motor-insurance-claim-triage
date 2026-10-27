"""Structure-aware chunking (L1).

Why not a fixed-size splitter
-----------------------------
A policy wording is not prose. It is a tree: chapters, sections, roman-numbered
clauses, lettered sub-clauses. A fixed-size splitter cuts across that tree, and
you end up with a chunk that contains the back half of an exclusion and the
front half of a definition. Retrieve it and you cite text that does not say what
you claim it says.

So the chunker works in three passes.

1. **Segment on the document's own headings.** Chapter, Section, numbered and
   lettered clause markers are recognised explicitly, and each leaf gets a
   `section_path` such as
   `Chapter 2 - Exception, Condition & Deductible > 1. GENERAL EXCEPTIONS > III`.

2. **Pack leaves into chunks without crossing a heading.** A leaf longer than
   the budget is split on sentence boundaries with overlap; short sibling leaves
   are packed together. A chunk therefore always belongs to exactly one section
   path.

3. **Tag and cross-link.** Each chunk is given a `role` -- grant, restriction,
   exclusion, condition, procedure or definition -- inferred from its heading
   path and its own wording, and a list of `references`: the names of other
   clauses it points at.

Pass 3 is what makes the section 4.1 case solvable. The general exceptions
clause says loss is excluded where the vehicle is "being used otherwise than in
accordance with the 'Limitations as to Use'". That chunk therefore records a
reference to the clause named *Limitations as to Use*, and the agent can follow
it by identifier instead of hoping an embedding of "food delivery accident"
lands near a sentence about hire and reward. The role tag gives the second lever:
a restriction-filtered search over the corpus, run deliberately after the grant
has been found.

Embedding text
--------------
What gets embedded is not the raw chunk. It is

    <document title> > <section path>
    [role: exclusion]
    <chunk text>

Policy clause bodies are full of unresolved references ("such person", "the
said occurrences"). Prefixing the heading path restores the context the body
assumes, and costs about twenty tokens.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Iterable

from .loaders import LoadedDocument

# -----------------------------------------------------------------------------
# Heading recognition
# -----------------------------------------------------------------------------

HEADING_PATTERNS: list[tuple[int, re.Pattern[str]]] = [
    # level 0: CHAPTER 4 - Limitations as to Use
    #          SECTION II - LIABILITY TO THIRD PARTIES
    #
    # The separator is mandatory, and `_is_title_like` then checks that what
    # follows reads as a title rather than as prose. Both constraints are
    # needed: without them the pattern also swallows mid-sentence
    # cross-references -- "Section II-I(C) but subject otherwise to the terms
    # exceptions" -- and the section tree collapses onto them.
    (0, re.compile(r"^\s*((?:CHAPTER|SECTION)\s+[0-9IVXLC]{1,5}\s*[-:.]\s*[\"']?[A-Za-z0-9].{0,70})$", re.IGNORECASE)),
    # level 1: 1. GENERAL EXCEPTIONS (Applicable to all Sections)
    (1, re.compile(r"^\s*(\d{1,2}\.\s+[A-Z][A-Z \-&'/(),]{6,90})$")),
    # level 2: roman numeral clause  I.  II.  III.
    (2, re.compile(r"^\s*([IVXLC]{1,5}\.)\s+\S")),
    # level 3: lettered sub-clause  a.  b)  (c)
    (3, re.compile(r"^\s*\(?([a-z])[.)]\s+\S")),
]

#: Named clauses the wording refers to by name from elsewhere. Recording these
#: is what lets the agent jump from an exception to the clause it qualifies.
NAMED_CLAUSES = [
    "Limitations as to Use",
    "Driver's Clause",
    "Drivers Clause",
    "General Exceptions",
    "Geographical Area",
    "Insured's Declared Value",
    "Deductible",
    "Claim Procedure",
    "Conditions",
    "Depreciation",
    "No Claim Bonus",
]

ROLE_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("exclusion", re.compile(r"\b(exception|exclusion|shall not be liable|not cover(ed)?|excluded)\b", re.I)),
    ("restriction", re.compile(r"\b(limitation[s]? as to use|hire or reward|carriage of goods|motor trade|organised racing|pace making|speed testing|restrict)\b", re.I)),
    ("procedure", re.compile(r"\b(claim procedure|intimat|survey(or)?|documents required|lodge|register (a|the) claim|cashless|reimbursement)\b", re.I)),
    ("condition", re.compile(r"\b(condition[s]?|precedent to|the insured shall|notice shall be given|duty of the insured)\b", re.I)),
    ("definition", re.compile(r"\b(means|shall mean|definition[s]?|is defined as|for the purpose of this)\b", re.I)),
    ("grant", re.compile(r"\b(the company will indemnify|the company shall indemnify|loss of or damage to|we will pay|is covered)\b", re.I)),
]

SENTENCE_SPLIT = re.compile(r"(?<=[.;:!?])\s+(?=[A-Z0-9(])")


def estimate_tokens(text: str) -> int:
    """Cheap token estimate: English legal prose runs ~4 characters per token.

    A real tokeniser would be more accurate, but it would also pin the chunker
    to one model family. The chunk budget is a soft target, so an estimate is
    the right level of precision here.
    """
    return max(1, len(text) // 4)


@dataclass
class Chunk:
    clause_id: str
    document_id: str
    document_title: str
    source_url: str
    section_path: str
    heading: str
    page: int
    ordinal: int
    role: str
    text: str
    embed_text: str
    references: list[str] = field(default_factory=list)
    token_estimate: int = 0
    content_sha256: str = ""

    def to_metadata(self) -> dict:
        data = asdict(self)
        data.pop("embed_text")
        data["references"] = "|".join(self.references)
        return data


@dataclass
class _Leaf:
    heading_path: list[str]
    text: str
    page: int


def _slug(text: str, limit: int = 48) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (cleaned[:limit] or "body").strip("-")


_ALL_CAPS_HEADING = re.compile(r"^\s*([A-Z][A-Z \-&'/(),.]{8,90})$")

#: Words that carry no capitalisation signal in a title.
_FUNCTION_WORDS = {
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or",
    "the", "to", "under", "with", "&", "-",
}
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'()]*")


def _is_title_like(text: str) -> bool:
    """Does this read as a heading rather than as a clause of prose?

    Headings capitalise their content words. Prose does not. Counting the
    capitalised share of content words separates `Exception, Condition &
    Deductible` from `I(C) but subject otherwise to the terms exceptions`
    without needing a list of every heading the document happens to contain.
    """
    words = [w for w in _WORD_RE.findall(text) if w.lower() not in _FUNCTION_WORDS]
    if not words:
        return False
    capitalised = sum(1 for w in words if w[0].isupper())
    return capitalised / len(words) >= 0.6


def _match_heading(line: str, previous: str | None) -> tuple[int, str] | None:
    """Classify a line as a heading, using the line above it as context.

    The context matters for the all-caps rule only. A policy wording is full of
    all-caps fragments that are the tail of a wrapped title or a defined term
    shouted mid-sentence; treating those as headings shreds the section tree.
    An all-caps line is accepted as a heading only when it starts a block --
    the line above it is absent, blank, or ends a sentence.
    """
    stripped = line.strip()
    if not stripped or len(stripped) > 110:
        return None
    for level, pattern in HEADING_PATTERNS:
        if pattern is _ALL_CAPS_HEADING:
            continue
        match = pattern.match(stripped)
        if match:
            if level == 0:
                title = re.sub(
                    r"^(?:CHAPTER|SECTION)\s+[0-9IVXLC]{1,5}\s*[-:.]\s*", "", stripped, flags=re.IGNORECASE
                )
                if not _is_title_like(title):
                    return None
                return 0, stripped.rstrip(" ,;")
            if level in (2, 3) and len(stripped) < 6:
                return None
            return level, stripped if level < 2 else match.group(1).rstrip(".)")

    match = _ALL_CAPS_HEADING.match(stripped)
    if match and (previous is None or not previous.strip() or previous.rstrip().endswith((".", ":", ";", "?", "!"))):
        return 1, match.group(1).strip()
    return None


def _segment(document: LoadedDocument) -> list[_Leaf]:
    leaves: list[_Leaf] = []
    path: list[str] = [document.title]
    buffer: list[str] = []
    page_of_buffer = 1

    def flush(page: int) -> None:
        nonlocal buffer
        body = "\n".join(buffer).strip()
        if body:
            leaves.append(_Leaf(heading_path=list(path), text=body, page=page))
        buffer = []

    for page in document.pages:
        lines = page.text.split("\n")
        for position, line in enumerate(lines):
            heading = _match_heading(line, lines[position - 1] if position else None)
            if heading is not None:
                level, label = heading
                flush(page_of_buffer)
                # levels are relative to the document title at index 0
                depth = level + 1
                path = path[:depth] + [label]
                page_of_buffer = page.number
                # a level-2/3 marker introduces text on the same line
                remainder = line.strip()
                if level >= 2:
                    remainder = re.sub(r"^\s*\(?[IVXLCa-z]{1,5}[.)]\s*", "", line.strip())
                    if remainder:
                        buffer.append(remainder)
                continue
            if not buffer:
                page_of_buffer = page.number
            buffer.append(line)
        flush(page_of_buffer)
    flush(page_of_buffer)
    return [leaf for leaf in leaves if leaf.text.strip()]


def _infer_role(section_path: str, text: str) -> str:
    haystack = f"{section_path}\n{text}"
    for role, pattern in ROLE_RULES:
        if pattern.search(haystack):
            return role
    return "grant"


def _find_references(text: str) -> list[str]:
    found = []
    for name in NAMED_CLAUSES:
        if re.search(re.escape(name), text, re.IGNORECASE):
            canonical = "Driver's Clause" if name == "Drivers Clause" else name
            if canonical not in found:
                found.append(canonical)
    return found


def _split_long(text: str, budget_tokens: int, overlap_tokens: int) -> list[str]:
    if estimate_tokens(text) <= budget_tokens:
        return [text]
    sentences = SENTENCE_SPLIT.split(text)
    pieces: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for sentence in sentences:
        sentence_tokens = estimate_tokens(sentence)
        if current and current_tokens + sentence_tokens > budget_tokens:
            pieces.append(" ".join(current).strip())
            # carry an overlap tail so a clause cut in two keeps its subject
            tail: list[str] = []
            tail_tokens = 0
            for prior in reversed(current):
                prior_tokens = estimate_tokens(prior)
                if tail_tokens + prior_tokens > overlap_tokens:
                    break
                tail.insert(0, prior)
                tail_tokens += prior_tokens
            current = tail
            current_tokens = tail_tokens
        current.append(sentence)
        current_tokens += sentence_tokens
    if current:
        pieces.append(" ".join(current).strip())
    return [p for p in pieces if p]


def chunk_document(
    document: LoadedDocument,
    *,
    chunk_tokens: int = 320,
    overlap_tokens: int = 64,
) -> list[Chunk]:
    leaves = _segment(document)
    chunks: list[Chunk] = []
    ordinal = 0

    pending: list[_Leaf] = []
    pending_tokens = 0

    def emit(group: Iterable[_Leaf]) -> None:
        nonlocal ordinal
        group = list(group)
        if not group:
            return
        path = group[0].heading_path
        section_path = " > ".join(path[1:]) or "(preamble)"
        heading = path[-1] if len(path) > 1 else document.title
        body = "\n".join(leaf.text for leaf in group).strip()
        page = group[0].page
        for piece in _split_long(body, chunk_tokens, overlap_tokens):
            role = _infer_role(section_path, piece)
            clause_id = f"{document.document_id}::{_slug(section_path)}::{ordinal:04d}"
            embed_text = (
                f"{document.title} > {section_path}\n"
                f"[role: {role}]\n"
                f"{piece}"
            )
            chunks.append(
                Chunk(
                    clause_id=clause_id,
                    document_id=document.document_id,
                    document_title=document.title,
                    source_url=document.source_url,
                    section_path=section_path,
                    heading=heading,
                    page=page,
                    ordinal=ordinal,
                    role=role,
                    text=piece,
                    embed_text=embed_text,
                    references=_find_references(piece),
                    token_estimate=estimate_tokens(piece),
                    content_sha256=hashlib.sha256(piece.encode("utf-8")).hexdigest()[:32],
                )
            )
            ordinal += 1

    for leaf in leaves:
        leaf_tokens = estimate_tokens(leaf.text)
        same_section = bool(pending) and pending[0].heading_path == leaf.heading_path
        if same_section and pending_tokens + leaf_tokens <= chunk_tokens:
            pending.append(leaf)
            pending_tokens += leaf_tokens
            continue
        emit(pending)
        pending = [leaf]
        pending_tokens = leaf_tokens
    emit(pending)

    return chunks
