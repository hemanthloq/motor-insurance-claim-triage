"""Geometric QA for the generated deck.

This machine has no LibreOffice and no Poppler, so the slides cannot be
rendered and inspected by eye. This is the substitute: it reads every shape's
real geometry out of the .pptx and checks the three defects a visual pass would
be looking for.

  1. text that does not fit its box      (the most common and most visible)
  2. anything outside the slide          (pptxgenjs writes it, PowerPoint hides it)
  3. text boxes overlapping each other   (two things rendered on top of each other)

Text height is *estimated* from an average-character-width model, so it is not
the same as a render. It is deliberately pessimistic -- it assumes a slightly
wider average glyph than Calibri and Cambria actually have -- so a box it
passes is very likely fine and a box it flags is worth looking at. Anything
within 15% of its bound is reported as tight rather than overflowing.

    python qa_deck.py claim-triage-deck.pptx
"""

from __future__ import annotations

import sys
from pathlib import Path

from pptx import Presentation
from pptx.util import Emu

SLIDE_W_IN = 13.333
SLIDE_H_IN = 7.5

#: Average glyph width as a fraction of point size. Measured generously:
#: Calibri runs about 0.46-0.48 for mixed-case English, Cambria about 0.50.
#: Using a higher number makes the estimator over-predict width, which is the
#: direction we want -- it will warn early rather than miss an overflow.
AVG_CHAR_W = {"Calibri": 0.50, "Cambria": 0.54, "Arial": 0.53}
DEFAULT_CHAR_W = 0.52
BOLD_FACTOR = 1.045
LINE_HEIGHT = 1.22  # multiple of point size
#: pptxgenjs text boxes carry internal padding unless margin:0 is set.
DEFAULT_INSET_IN = 0.1

TIGHT = 0.85  # fraction of the box above which we call it tight


def emu_in(value) -> float:
    return Emu(value).inches if value is not None else 0.0


def wrap_lines(text: str, chars_per_line: int) -> int:
    """How many lines does this text occupy at this many characters per line?"""
    lines = 0
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            lines += 1
            continue
        current, count = 0, 1
        for word in words:
            need = len(word) + (1 if current else 0)
            if current + need > chars_per_line and current:
                count += 1
                current = len(word)
            else:
                current += need
        lines += count
    return max(1, lines)


def estimate_height(shape, box_w_in: float) -> tuple[float, int, float]:
    """Estimated rendered height, line count, and the dominant point size.

    Measured per paragraph rather than per shape. Taking the largest font in a
    shape and applying it to all of its text -- which an earlier version did --
    reports a heading-plus-body block as needing twice the room it does, and
    then you spend the afternoon "fixing" slides that were already fine.
    """
    total_h = 0.0
    total_lines = 0
    biggest = 0.0
    for para in shape.text_frame.paragraphs:
        runs = [r for r in para.runs if r.text]
        if not runs:
            total_h += 11.0 * LINE_HEIGHT / 72.0  # empty paragraph = a blank line
            total_lines += 1
            continue
        # Width is driven by the weighted average glyph across the paragraph's
        # runs; line height by its largest font.
        weighted_w, chars, para_pt = 0.0, 0, 0.0
        for run in runs:
            pt = run.font.size.pt if run.font.size is not None else 14.0
            face = run.font.name or "Calibri"
            bold = bool(run.font.bold)
            w = AVG_CHAR_W.get(face, DEFAULT_CHAR_W) * pt * (BOLD_FACTOR if bold else 1.0)
            weighted_w += w * len(run.text)
            chars += len(run.text)
            para_pt = max(para_pt, pt)
        avg_w = weighted_w / max(1, chars)
        chars_per_line = max(1, int((box_w_in * 72.0) / avg_w))
        text = "".join(r.text for r in runs)
        lines = wrap_lines(text, chars_per_line)
        total_lines += lines
        total_h += lines * para_pt * LINE_HEIGHT / 72.0
        biggest = max(biggest, para_pt)
    return total_h, max(1, total_lines), biggest or 14.0


#: Placeholders whose content is a page number or similar one-token field --
#: never an overflow risk, and their run has no explicit size so the estimator
#: would read the 18pt default and flag every slide.
SKIP_NAMES = ("Slide Number Placeholder",)


def shape_rows(slide):
    for shape in slide.shapes:
        if not shape.has_text_frame:
            continue
        if any(shape.name.startswith(prefix) for prefix in SKIP_NAMES):
            continue
        text = shape.text_frame.text
        if not text.strip():
            continue
        yield shape, text


def main(path: str) -> int:
    pres = Presentation(path)
    problems: list[str] = []
    tight: list[str] = []
    checked = 0

    for index, slide in enumerate(pres.slides, start=1):
        boxes = []
        for shape, text in shape_rows(slide):
            checked += 1
            x, y = emu_in(shape.left), emu_in(shape.top)
            w, h = emu_in(shape.width), emu_in(shape.height)
            name = shape.name or f"shape#{shape.shape_id}"

            # -- 2. outside the slide ---------------------------------------
            if x < -0.01 or y < -0.01 or x + w > SLIDE_W_IN + 0.01 or y + h > SLIDE_H_IN + 0.01:
                problems.append(
                    f"  slide {index:>2}  OFF-SLIDE   {name}: "
                    f"({x:.2f},{y:.2f}) {w:.2f}x{h:.2f} in a {SLIDE_W_IN}x{SLIDE_H_IN} slide"
                )

            # -- margins ----------------------------------------------------
            if 0 <= x < 0.5 and w > 0.4:
                problems.append(f"  slide {index:>2}  MARGIN      {name}: left edge at {x:.2f}in (< 0.5in)")

            # -- 1. text fit -------------------------------------------------
            inset = 0.0
            try:
                inset = emu_in(shape.text_frame.margin_left) + emu_in(shape.text_frame.margin_right)
            except Exception:
                inset = DEFAULT_INSET_IN * 2
            usable_w = max(0.2, w - inset)
            needed_h, lines, pt = estimate_height(shape, usable_w)
            try:
                v_inset = emu_in(shape.text_frame.margin_top) + emu_in(shape.text_frame.margin_bottom)
            except Exception:
                v_inset = 0.1
            usable_h = max(0.1, h - v_inset)
            ratio = needed_h / usable_h
            excerpt = text.replace("\n", " ")[:58]
            if ratio > 1.0:
                problems.append(
                    f"  slide {index:>2}  OVERFLOW    {name}: needs ~{needed_h:.2f}in "
                    f"({lines} lines @ {pt:.0f}pt) in {usable_h:.2f}in  | {excerpt!r}"
                )
            elif ratio > TIGHT:
                tight.append(
                    f"  slide {index:>2}  tight       {name}: ~{ratio * 100:.0f}% full "
                    f"({lines} lines @ {pt:.0f}pt)  | {excerpt!r}"
                )

            boxes.append((name, x, y, w, h))

        # -- 3. overlapping text boxes --------------------------------------
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                n1, x1, y1, w1, h1 = boxes[i]
                n2, x2, y2, w2, h2 = boxes[j]
                ox = min(x1 + w1, x2 + w2) - max(x1, x2)
                oy = min(y1 + h1, y2 + h2) - max(y1, y2)
                if ox > 0.06 and oy > 0.06:
                    area = ox * oy
                    smaller = min(w1 * h1, w2 * h2)
                    if smaller and area / smaller > 0.35:
                        problems.append(
                            f"  slide {index:>2}  OVERLAP     {n1} x {n2}: "
                            f"{ox:.2f}x{oy:.2f}in ({area / smaller * 100:.0f}% of the smaller)"
                        )

    print(f"\nchecked {checked} text shapes across {len(pres.slides)} slides\n")
    if problems:
        print(f"PROBLEMS ({len(problems)})")
        for line in problems:
            print(line)
    else:
        print("no overflow, off-slide or overlap problems found")
    if tight:
        print(f"\nTIGHT, worth a look ({len(tight)})")
        for line in tight:
            print(line)
    print()
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "claim-triage-deck.pptx"))
