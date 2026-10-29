/* =============================================================================
 *  Presentation deck generator
 *
 *      cd docs/presentation && npm install pptxgenjs && node build_deck.js
 *
 *  Produces claim-triage-deck.pptx. The deck is generated rather than hand-made
 *  so that every number on it comes from one place and can be re-checked
 *  against the repository -- the figures below are lifted from
 *  docs/evaluation.md, docs/load-test.md, docs/cost.md and INCIDENT_LOG.md.
 * ============================================================================= */

const pptxgen = require("pptxgenjs");
const path = require("path");

// -----------------------------------------------------------------------------
//  Theme
//
//  Deep indigo dominates; terracotta is the single sharp accent. The green /
//  amber / terracotta trio is not decoration -- it is the routing semantics the
//  system actually emits (fast-track, surveyor, investigation), so the deck's
//  colours mean the same thing on every slide they appear.
//
//  Cambria for headings: the deck is about reading a legal document, and a
//  serif says so. Calibri body. Both are on the QA-safe list.
// -----------------------------------------------------------------------------
const THEME = {
  name: "Claim Triage",
  headFontFace: "Cambria",
  bodyFontFace: "Calibri",
  colors: {
    dk1: "16181F", // body text on light
    lt1: "FFFFFF",
    dk2: "232738", // dark slide background
    lt2: "F4F5F8", // card tint on light slides
    accent1: "B85042", // terracotta -- the signature, and "investigation"
    accent2: "E0A458", // amber -- "surveyor"
    accent3: "3F7D6E", // deep sage -- "fast-track" / verified
    accent4: "5A6378", // slate -- muted text
    accent5: "A7BEAE", // light sage
    accent6: "8C3B30", // dark terracotta
    hlink: "B85042",
    folHlink: "8C3B30",
  },
};

const HEX = THEME.colors;
const pres = new pptxgen();
const C = pres.SchemeColor;

pres.layout = "LAYOUT_WIDE"; // 13.3in x 7.5in -- set BEFORE any slide is added
pres.theme = { headFontFace: THEME.headFontFace, bodyFontFace: THEME.bodyFontFace };
pres.author = "Mini Project 01";
pres.company = "Dept of CSE (AI &amp; ML), PESU"; // ampersand must be escaped here
pres.subject = "Motor Insurance Claim Intake and Triage";
pres.title = "Motor Insurance Claim Intake and Triage";

const W = 13.3;
const H = 7.5;
const M = 0.62; // side margin
const CW = W - M * 2; // content width = 12.06

// -----------------------------------------------------------------------------
//  Layouts
// -----------------------------------------------------------------------------

pres.defineSlideMaster({
  title: "TITLE_DARK",
  background: { color: C.text2 },
  objects: [
    {
      placeholder: {
        options: { name: "eyebrow", type: "body", x: M, y: 2.05, w: CW, h: 0.4,
          fontSize: 14, bold: true, charSpacing: 3, color: HEX.accent2, align: "left" },
        text: "",
      },
    },
    {
      placeholder: {
        options: { name: "title", type: "title", x: M, y: 2.5, w: CW, h: 1.92,
          fontSize: 46, bold: true, color: C.background1, align: "left", valign: "top" },
        text: "",
      },
    },
    {
      placeholder: {
        options: { name: "body", type: "body", x: M, y: 4.35, w: 9.4, h: 1.5,
          fontSize: 17, color: "A7BEAE", align: "left", valign: "top" },
        text: "",
      },
    },
  ],
});

pres.defineSlideMaster({
  title: "SECTION_DARK",
  background: { color: C.text2 },
  objects: [
    {
      placeholder: {
        options: { name: "eyebrow", type: "body", x: M, y: 2.9, w: CW, h: 0.4,
          fontSize: 13, bold: true, charSpacing: 3, color: HEX.accent2 },
        text: "",
      },
    },
    {
      placeholder: {
        options: { name: "title", type: "title", x: M, y: 3.32, w: CW, h: 1.1,
          fontSize: 40, bold: true, color: C.background1 },
        text: "",
      },
    },
    {
      placeholder: {
        options: { name: "body", type: "body", x: M, y: 4.45, w: 9.6, h: 0.9,
          fontSize: 16, color: "A7BEAE" },
        text: "",
      },
    },
  ],
});

pres.defineSlideMaster({
  title: "CONTENT",
  background: { color: C.background1 },
  objects: [
    {
      placeholder: {
        options: { name: "title", type: "title", x: M, y: 0.42, w: CW, h: 0.88,
          fontSize: 32, bold: true, color: C.text2, align: "left", valign: "middle" },
        text: "",
      },
    },
    {
      placeholder: {
        options: { name: "body", type: "body", x: M, y: 1.26, w: CW, h: 5.5,
          fontSize: 15, color: C.text1, align: "left", valign: "top" },
        text: "",
      },
    },
    { text: { text: "Motor Insurance Claim Intake and Triage", options: {
        x: M, y: 6.96, w: 7.5, h: 0.3, fontSize: 9.5, color: HEX.accent4, isTextBox: true } } },
  ],
  slideNumber: { x: 12.5, y: 6.96, w: 0.5, h: 0.3, fontSize: 9.5, color: HEX.accent4, align: "right" },
});

pres.defineSlideMaster({
  title: "CONTENT_DARK",
  background: { color: C.text2 },
  objects: [
    {
      placeholder: {
        options: { name: "title", type: "title", x: M, y: 0.42, w: CW, h: 0.88,
          fontSize: 32, bold: true, color: C.background1, align: "left", valign: "middle" },
        text: "",
      },
    },
    {
      placeholder: {
        options: { name: "body", type: "body", x: M, y: 1.26, w: CW, h: 5.5,
          fontSize: 15, color: "D8DCE6", align: "left", valign: "top" },
        text: "",
      },
    },
    { text: { text: "Motor Insurance Claim Intake and Triage", options: {
        x: M, y: 6.96, w: 7.5, h: 0.3, fontSize: 9.5, color: "6E7689", isTextBox: true } } },
  ],
  slideNumber: { x: 12.5, y: 6.96, w: 0.5, h: 0.3, fontSize: 9.5, color: "6E7689", align: "right" },
});

pres.defineSlideMaster({
  title: "CHART_SLIDE",
  background: { color: C.background1 },
  objects: [
    {
      placeholder: {
        options: { name: "title", type: "title", x: M, y: 0.42, w: CW, h: 0.88,
          fontSize: 32, bold: true, color: C.text2, align: "left", valign: "middle" },
        text: "",
      },
    },
    {
      placeholder: {
        options: { name: "chart", type: "chart", x: M, y: 1.4, w: 7.5, h: 4.9, color: C.text1 },
        text: "",
      },
    },
    {
      placeholder: {
        options: { name: "body", type: "body", x: 8.42, y: 1.4, w: 4.26, h: 4.9,
          fontSize: 14, color: C.text1, valign: "top" },
        text: "",
      },
    },
    { text: { text: "Motor Insurance Claim Intake and Triage", options: {
        x: M, y: 6.96, w: 7.5, h: 0.3, fontSize: 9.5, color: HEX.accent4, isTextBox: true } } },
  ],
  slideNumber: { x: 12.5, y: 6.96, w: 0.5, h: 0.3, fontSize: 9.5, color: HEX.accent4, align: "right" },
});

// -----------------------------------------------------------------------------
//  Composition helpers
// -----------------------------------------------------------------------------

/** A tinted card. Returns nothing; draws onto the slide. */
function card(slide, { x, y, w, h, fill = C.background2, line = null, name }) {
  const opts = {
    x, y, w, h,
    fill: { color: fill },
    rectRadius: 0.08,
    objectName: name,
  };
  if (line) opts.line = { color: line, width: 1 };
  slide.addShape(pres.shapes.ROUNDED_RECTANGLE, opts);
}

/** A numbered circle + heading + body, used for the "three mechanisms" rows. */
function numberedRow(slide, { n, x, y, w, heading, body, accent = C.accent1, dark = false }) {
  slide.addShape(pres.shapes.OVAL, {
    x, y, w: 0.46, h: 0.46, fill: { color: accent }, objectName: `num-${n}-dot`,
  });
  slide.addText(String(n), {
    x, y, w: 0.46, h: 0.46, align: "center", valign: "middle",
    fontSize: 16, bold: true, color: "FFFFFF", margin: 0, isTextBox: true,
    objectName: `num-${n}-label`,
  });
  slide.addText(heading, {
    x: x + 0.66, y: y - 0.02, w: w - 0.66, h: 0.36,
    fontSize: 16, bold: true, color: dark ? "FFFFFF" : C.text2, margin: 0,
    isTextBox: true, objectName: `num-${n}-heading`,
  });
  slide.addText(body, {
    x: x + 0.66, y: y + 0.34, w: w - 0.66, h: 0.86,
    fontSize: 13, color: dark ? "C2C8D6" : C.text1, margin: 0, lineSpacingMultiple: 1.12,
    isTextBox: true, objectName: `num-${n}-body`,
  });
}

/** A big number with a caption under it. */
function stat(slide, { x, y, w, value, label, color = C.accent1, size = 40, name }) {
  slide.addText(value, {
    x, y, w, h: 0.74, fontSize: size, bold: true, color, margin: 0,
    align: "left", valign: "bottom", isTextBox: true, objectName: `${name}-value`,
  });
  slide.addText(label, {
    x, y: y + 0.76, w, h: 0.62, fontSize: 11.5, color: HEX.accent4, margin: 0,
    align: "left", valign: "top", lineSpacingMultiple: 1.1,
    isTextBox: true, objectName: `${name}-label`,
  });
}

/** A policy-clause card -- the deck's repeated visual motif. */
function clauseCard(slide, { x, y, w, h, where, page, quote, role, roleColor, name }) {
  card(slide, { x, y, w, h, fill: "2E3346", name: `${name}-card` });
  slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
    x: x + 0.28, y: y + 0.24, w: 1.42, h: 0.3,
    fill: { color: roleColor }, rectRadius: 0.14, objectName: `${name}-roletag`,
  });
  slide.addText(role, {
    x: x + 0.28, y: y + 0.24, w: 1.42, h: 0.3, align: "center", valign: "middle",
    fontSize: 9.5, bold: true, color: "FFFFFF", charSpacing: 1, margin: 0,
    isTextBox: true, objectName: `${name}-rolelabel`,
  });
  // The citation sits on its own row rather than beside the role tag: beside
  // it there are only ~1.8in left, and "Chapter 2, Gen. Exc. III(a) · page 3"
  // wraps to two lines inside a one-line box.
  slide.addText(`${where}  ·  page ${page}`, {
    x: x + 0.28, y: y + 0.6, w: w - 0.56, h: 0.3, fontSize: 11.5, bold: true,
    color: "A7BEAE", valign: "middle", margin: 0,
    isTextBox: true, objectName: `${name}-where`,
  });
  slide.addText(quote, {
    x: x + 0.28, y: y + 0.96, w: w - 0.56, h: h - 1.2, fontSize: 13, italic: true,
    color: "EDEFF4", lineSpacingMultiple: 1.14, margin: 0,
    isTextBox: true, objectName: `${name}-quote`,
  });
}

const CHART_BASE = {
  showTitle: false,
  showLegend: false,
  catAxisLabelColor: HEX.accent4,
  valAxisLabelColor: HEX.accent4,
  catAxisLabelFontFace: "+mn-lt",
  valAxisLabelFontFace: "+mn-lt",
  catAxisLabelFontSize: 11,
  valAxisLabelFontSize: 11,
  dataLabelFontFace: "+mn-lt",
  dataLabelFontSize: 11,
  dataLabelColor: HEX.dk1,
  valGridLine: { color: "E3E5EC", size: 1 },
  catGridLine: { style: "none" },
  showValue: true,
};

// =============================================================================
//  1 -- Title
// =============================================================================
pres.addSection({ title: "Opening" });
{
  const s = pres.addSlide({ masterName: "TITLE_DARK", sectionTitle: "Opening" });
  s.addText("UE24AM342AA5  ·  ML SYSTEM DESIGN AND AGENTOPS  ·  MINI PROJECT 01", {
    placeholder: "eyebrow",
  });
  s.addText("Motor Insurance Claim\nIntake and Triage", { placeholder: "title" });
  s.addText(
    "A multi-agent service that reads an insurer's own policy wording, states a coverage position with clause-level citations, and routes the claim — or escalates when it cannot support an answer.",
    { placeholder: "body" }
  );

  // Advisory notice: the single most important claim the system makes about itself.
  card(s, { x: M, y: 5.95, w: 8.1, h: 0.78, fill: "2E3346", name: "advisory-card" });
  s.addShape(pres.shapes.OVAL, { x: M + 0.26, y: 6.18, w: 0.32, h: 0.32,
    fill: { color: C.accent2 }, objectName: "advisory-dot" });
  s.addText("!", { x: M + 0.26, y: 6.18, w: 0.32, h: 0.32, align: "center", valign: "middle",
    fontSize: 15, bold: true, color: "232738", margin: 0, isTextBox: true, objectName: "advisory-bang" });
  s.addText("Advisory only. The system never approves, rejects or values a claim. A human claims handler decides.", {
    x: M + 0.72, y: 5.95, w: 7.2, h: 0.78, fontSize: 13, color: "EDEFF4",
    valign: "middle", margin: 0, isTextBox: true, objectName: "advisory-text",
  });

  s.addText(
    [
      { text: "Corpus\n", options: { fontSize: 10, bold: true, color: "E0A458", charSpacing: 2 } },
      { text: "ICICI Lombard Private Car Package Policy\nProduct 3001 · UIN IRDAN115RP0017V01200102\n46 pages + 8 page claims document · 381 indexed clauses", options: { fontSize: 11.5, color: "A7BEAE" } },
    ],
    { x: 9.1, y: 5.9, w: 3.6, h: 1.1, align: "left", margin: 0, isTextBox: true,
      lineSpacingMultiple: 1.2, objectName: "corpus-block" }
  );
  s.addNotes(
    "Frame the whole talk in one sentence: this is a retrieval problem disguised as a classification problem. " +
    "Say the advisory line out loud — it is a design constraint enforced by an output guardrail, not a disclaimer."
  );
}

// =============================================================================
//  2 -- The problem
// =============================================================================
pres.addSection({ title: "The problem" });
{
  const s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "The problem" });
  s.addText("What a claims handler actually does", { placeholder: "title" });

  const steps = [
    ["Check", "Is the submission complete and internally consistent?"],
    ["Classify", "Own damage, third party, theft, or total loss?"],
    ["Look up", "What does the policy wording actually say about it?"],
    ["Route", "Fast-track, surveyor, or investigation — and why?"],
  ];
  steps.forEach(([head, body], i) => {
    const x = M + i * 3.07;
    card(s, { x, y: 1.4, w: 2.82, h: 1.95, name: `step-${i}` });
    s.addText(String(i + 1).padStart(2, "0"), {
      x: x + 0.26, y: 1.6, w: 1.0, h: 0.5, fontSize: 24, bold: true,
      color: C.accent1, margin: 0, isTextBox: true, objectName: `step-${i}-num`,
    });
    s.addText(head, {
      x: x + 0.26, y: 2.1, w: 2.3, h: 0.34, fontSize: 16, bold: true,
      color: C.text2, margin: 0, isTextBox: true, objectName: `step-${i}-head`,
    });
    s.addText(body, {
      x: x + 0.26, y: 2.46, w: 2.3, h: 0.76, fontSize: 12, color: C.text1,
      margin: 0, lineSpacingMultiple: 1.12, isTextBox: true, objectName: `step-${i}-body`,
    });
  });

  s.addText("This service does the first three and recommends the fourth.", {
    x: M, y: 3.62, w: CW, h: 0.4, fontSize: 18, bold: true, color: C.text2,
    margin: 0, isTextBox: true, objectName: "scope-line",
  });

  card(s, { x: M, y: 4.2, w: 5.88, h: 2.25, fill: "F7EDEB", name: "why-hard" });
  s.addText("Why it is hard to automate", {
    x: M + 0.3, y: 4.42, w: 5.3, h: 0.34, fontSize: 15, bold: true, color: C.accent6,
    margin: 0, isTextBox: true, objectName: "why-hard-head",
  });
  s.addText(
    [
      { text: "High volume, latency-sensitive, correctness-critical.", options: { bullet: true, breakLine: true } },
      { text: "Getting the coverage position wrong is a regulatory and financial problem.", options: { bullet: true, breakLine: true } },
      { text: "The FNOL is mostly free text, written by someone who has just had an accident.", options: { bullet: true } },
    ],
    { x: M + 0.3, y: 4.8, w: 5.3, h: 1.5, fontSize: 12.5, color: C.text1,
      paraSpaceAfter: 6, margin: 0, isTextBox: true, objectName: "why-hard-list" }
  );

  card(s, { x: 6.8, y: 4.2, w: 5.88, h: 2.25, fill: "EBF1EF", name: "second-req" });
  s.addText("And a second requirement, less obvious", {
    x: 7.1, y: 4.42, w: 5.3, h: 0.34, fontSize: 15, bold: true, color: C.accent3,
    margin: 0, isTextBox: true, objectName: "second-req-head",
  });
  s.addText(
    "A system that always answers is easy to build. One that knows when not to answer is not.\n\nEverything in this design that looks like over-engineering exists to make the second kind possible.",
    { x: 7.1, y: 4.8, w: 5.3, h: 1.5, fontSize: 13, color: C.text1,
      lineSpacingMultiple: 1.14, margin: 0, isTextBox: true, objectName: "second-req-body" }
  );
  s.addNotes("Keep this slide short — 45 seconds. The next slide is the one that matters.");
}

// =============================================================================
//  3 -- The hard case
// =============================================================================
pres.addSection({ title: "The hard case" });
{
  const s = pres.addSlide({ masterName: "CONTENT_DARK", sectionTitle: "The hard case" });
  s.addText("One sentence, three clauses, two chapters apart", { placeholder: "title" });

  card(s, { x: M, y: 1.22, w: CW, h: 0.82, fill: "3A2F2E", name: "quote-band" });
  s.addText(
    [
      { text: "“I was making a food delivery run in my own car when the accident happened.”", options: { italic: true } },
    ],
    { x: M + 0.34, y: 1.22, w: CW - 0.68, h: 0.82, fontSize: 19, color: "F3E3DF",
      valign: "middle", margin: 0, isTextBox: true, objectName: "the-sentence" }
  );

  clauseCard(s, {
    x: M, y: 2.2, w: 3.9, h: 2.5, where: "Chapter 1, Section I", page: 1,
    role: "GRANT", roleColor: HEX.accent3, name: "c1",
    quote: "“The Company will indemnify the insured against loss or damage to the vehicle insured … by accidental external means.”",
  });
  clauseCard(s, {
    x: M + 4.08, y: 2.2, w: 3.9, h: 2.5, where: "Chapter 4", page: 19,
    role: "RESTRICTION", roleColor: HEX.accent1, name: "c2",
    quote: "“The Policy covers use of the vehicle for any purpose other than: Hire or Reward, Carriage of goods …”",
  });
  clauseCard(s, {
    x: M + 8.16, y: 2.2, w: 3.9, h: 2.5, where: "Chapter 2, Gen. Exc. III(a)", page: 3,
    role: "THE BRIDGE", roleColor: HEX.accent2, name: "c3",
    quote: "“… shall not be liable … whilst the vehicle is being used otherwise than in accordance with the ‘Limitations as to Use’.”",
  });

  s.addText("Retrieved by any search", {
    x: M, y: 4.74, w: 3.9, h: 0.3, fontSize: 11, color: "8FA3B8", align: "center",
    margin: 0, isTextBox: true, objectName: "c1-note",
  });
  s.addText("Shares almost no words with the claim", {
    x: M + 4.08, y: 4.74, w: 3.9, h: 0.3, fontSize: 11, color: "8FA3B8", align: "center",
    margin: 0, isTextBox: true, objectName: "c2-note",
  });
  s.addText("Names the restriction — the way in", {
    x: M + 8.16, y: 4.74, w: 3.9, h: 0.3, fontSize: 11, color: "8FA3B8", align: "center",
    margin: 0, isTextBox: true, objectName: "c3-note",
  });

  card(s, { x: M, y: 5.24, w: CW, h: 1.44, fill: "2E3346", name: "sofar" });
  s.addText("The restriction does not contain the words accident, damage, bumper, claim, cover, delivery or food.", {
    x: M + 0.34, y: 5.42, w: CW - 0.68, h: 0.34, fontSize: 15, bold: true, color: "FFFFFF",
    margin: 0, isTextBox: true, objectName: "sofar-head",
  });
  s.addText(
    "It shares exactly one content word with the request — and it does not even use that word. No phrasing of the customer's sentence has an embedding that lands near it. This is not a tuning problem; it is structural.",
    { x: M + 0.34, y: 5.8, w: CW - 0.68, h: 0.72, fontSize: 13, color: "C2C8D6",
      lineSpacingMultiple: 1.1, margin: 0, isTextBox: true, objectName: "sofar-body" }
  );
  s.addNotes(
    "Slow down here. Read the sentence, then the three clauses, in order. The point to land: the clause that decides the " +
    "outcome is the one the question cannot reach. Everything after this slide is a consequence of that."
  );
}

// =============================================================================
//  4 -- Why one retrieval pass fails
// =============================================================================
{
  const s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "The hard case" });
  s.addText("Why retrieve-then-answer gets this wrong", { placeholder: "title" });

  card(s, { x: M, y: 1.34, w: 5.86, h: 2.5, fill: "F7EDEB", name: "naive" });
  s.addText("Retrieve → answer", {
    x: M + 0.3, y: 1.56, w: 5.26, h: 0.36, fontSize: 17, bold: true, color: C.accent6,
    margin: 0, isTextBox: true, objectName: "naive-head",
  });
  s.addText(
    [
      { text: "Embed the request", options: { bullet: true, breakLine: true } },
      { text: "Retrieve the highest-scoring passages", options: { bullet: true, breakLine: true } },
      { text: "Ask the model to answer from them", options: { bullet: true, breakLine: true } },
      { text: "Returns the own-damage grant. Says “covered”.", options: { bullet: true, color: "8C3B30", bold: true } },
    ],
    { x: M + 0.3, y: 1.98, w: 5.26, h: 1.72, fontSize: 13.5, color: C.text1,
      paraSpaceAfter: 7, margin: 0, isTextBox: true, objectName: "naive-list" }
  );

  card(s, { x: 6.82, y: 1.34, w: 5.86, h: 2.5, fill: "EBF1EF", name: "agentic" });
  s.addText("What an agent has to do instead", {
    x: 7.12, y: 1.56, w: 5.26, h: 0.36, fontSize: 17, bold: true, color: C.accent3,
    margin: 0, isTextBox: true, objectName: "agentic-head",
  });
  s.addText(
    [
      { text: "Retrieve the provision that appears to apply", options: { bullet: true, breakLine: true } },
      { text: "Ask whether anything else modifies it", options: { bullet: true, bold: true, breakLine: true } },
      { text: "Search again, in the policy's own vocabulary", options: { bullet: true, breakLine: true } },
      { text: "Verify the answer against what was retrieved", options: { bullet: true, bold: true, breakLine: true } },
      { text: "Retry differently, or escalate", options: { bullet: true } },
    ],
    { x: 7.12, y: 1.98, w: 5.26, h: 1.72, fontSize: 13.5, color: C.text1,
      paraSpaceAfter: 5, margin: 0, isTextBox: true, objectName: "agentic-list" }
  );

  s.addText("Steps 2 and 4 are what distinguish an agent from a fixed pipeline. Both are implemented as graph nodes that can change the run's course.", {
    x: M, y: 4.02, w: CW, h: 0.58, fontSize: 14.5, italic: true, color: C.accent4,
    margin: 0, isTextBox: true, objectName: "steps-note",
  });

  card(s, { x: M, y: 4.62, w: CW, h: 1.9, fill: C.text2, name: "root-cause" });
  s.addText("The root cause, stated precisely", {
    x: M + 0.34, y: 4.84, w: CW - 0.68, h: 0.34, fontSize: 15, bold: true, color: "E0A458",
    margin: 0, isTextBox: true, objectName: "root-head",
  });
  s.addText(
    "A single retrieval pass returns passages that resemble the question asked. This case turns on a provision the request never mentions — so no amount of better embedding, reranking or prompt engineering reaches it. The fix has to change what is searched for, not how well.",
    { x: M + 0.34, y: 5.22, w: CW - 0.68, h: 1.1, fontSize: 14, color: "EDEFF4",
      lineSpacingMultiple: 1.16, margin: 0, isTextBox: true, objectName: "root-body" }
  );
  s.addNotes("If asked 'why not just use a bigger model' — this slide is the answer. The information is not in the retrieved set.");
}

// =============================================================================
//  5 -- Three mechanisms
// =============================================================================
pres.addSection({ title: "Design" });
{
  const s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Design" });
  s.addText("Three mechanisms, each built for that case", { placeholder: "title" });

  numberedRow(s, {
    n: 1, x: M, y: 1.36, w: CW, accent: C.accent1,
    heading: "Cross-references recorded at index time",
    body: "The general exception names the restriction — “otherwise than in accordance with the ‘Limitations as to Use’”. The chunker detects that and stores the link, so the agent can follow it by name instead of hoping a vector search lands on it.",
  });
  numberedRow(s, {
    n: 2, x: M, y: 2.84, w: CW, accent: C.accent2,
    heading: "Every clause tagged by role",
    body: "grant · restriction · exclusion · condition · procedure · definition. This makes a role-filtered sweep possible: “what, anywhere in this wording, could take the cover away?” — a question no phrasing of the claim will answer.",
  });
  numberedRow(s, {
    n: 3, x: M, y: 4.32, w: CW, accent: C.accent3,
    heading: "A planning step that decides whether to look",
    body: "After the grant is found, the Coverage Analyst is asked whether each of four things could displace it, and must write its queries in the policy's vocabulary. It is allowed to answer “nothing applies” and stop — which is what makes retrieval a tool it chooses, not a stage it passes through.",
  });

  card(s, { x: M, y: 5.86, w: CW, h: 0.82, fill: C.background2, name: "mech-foot" });
  s.addText(
    [
      { text: "“Food delivery” will not match the clause.  ", options: { color: "8C3B30" } },
      { text: "“Hire or reward”, “carriage of goods” and “limitations as to use” will.", options: { bold: true, color: "3F7D6E" } },
    ],
    { x: M + 0.34, y: 5.86, w: CW - 0.68, h: 0.82, fontSize: 14, valign: "middle",
      margin: 0, isTextBox: true, objectName: "mech-foot-text" }
  );
  s.addNotes("That last line is lifted verbatim from the v2 query-formulation prompt. It is the design in one sentence.");
}

// =============================================================================
//  6 -- Agent topology
// =============================================================================
{
  const s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Design" });
  s.addText("Four agents, and a loop that can send work back", { placeholder: "title" });

  const agents = [
    ["Intake Analyst", "Classify, extract entities, flag the\ndeclared-use discrepancy, propose\nthe first queries", HEX.accent4],
    ["Coverage Analyst", "ground_search → plan_modifiers →\ntargeted_search → analyse.\nOwns all five tools", HEX.accent2],
    ["Verifier", "Quotes resolve? Claims entailed?\nThe only node that can send\nwork back", HEX.accent1],
    ["Routing Officer", "Chooses the queue — then the\ndeterministic signals raise it to\ntheir floor", HEX.accent3],
  ];
  agents.forEach(([name, body, col], i) => {
    const x = M + i * 3.07;
    card(s, { x, y: 1.34, w: 2.82, h: 2.1, name: `agent-${i}` });
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, {
      x, y: 1.34, w: 2.82, h: 0.46, fill: { color: col }, rectRadius: 0.08,
      objectName: `agent-${i}-cap`,
    });
    s.addText(name, {
      x: x + 0.16, y: 1.34, w: 2.5, h: 0.46, fontSize: 13.5, bold: true, color: "FFFFFF",
      valign: "middle", margin: 0, isTextBox: true, objectName: `agent-${i}-name`,
    });
    s.addText(body, {
      x: x + 0.22, y: 1.92, w: 2.4, h: 1.4, fontSize: 11.5, color: C.text1,
      lineSpacingMultiple: 1.14, margin: 0, isTextBox: true, objectName: `agent-${i}-body`,
    });
    if (i < 3) {
      s.addText("›", { x: x + 2.82, y: 1.34, w: 0.25, h: 2.1, fontSize: 24, bold: true,
        color: HEX.accent4, align: "center", valign: "middle", margin: 0,
        isTextBox: true, objectName: `agent-arrow-${i}` });
    }
  });

  s.addText("unsupported  →  back to the Coverage Analyst with a different search strategy  →  or escalate", {
    x: M, y: 3.56, w: CW, h: 0.34, fontSize: 12.5, bold: true, color: C.accent1,
    align: "center", margin: 0, isTextBox: true, objectName: "loop-label",
  });

  card(s, { x: M, y: 4.06, w: 5.86, h: 2.5, fill: C.background2, name: "why-agents" });
  s.addText("Why four agents and not four prompts", {
    x: M + 0.3, y: 4.28, w: 5.26, h: 0.34, fontSize: 15, bold: true, color: C.text2,
    margin: 0, isTextBox: true, objectName: "why-agents-head",
  });
  s.addText(
    "Each owns part of the state and can fail independently. The Verifier is adversarial by construction: it receives the passages and the draft, not the Analyst's reasoning, and it is the only node with the authority to loop.\n\nThe Routing Officer is separate because part of its decision is not the model's at all.",
    { x: M + 0.3, y: 4.66, w: 5.26, h: 1.7, fontSize: 12.5, color: C.text1,
      lineSpacingMultiple: 1.14, margin: 0, isTextBox: true, objectName: "why-agents-body" }
  );

  card(s, { x: 6.82, y: 4.06, w: 5.86, h: 2.5, fill: C.background2, name: "bounds" });
  s.addText("Bounded three ways, independently", {
    x: 7.12, y: 4.28, w: 5.26, h: 0.34, fontSize: 15, bold: true, color: C.text2,
    margin: 0, isTextBox: true, objectName: "bounds-head",
  });
  [["14", "steps"], ["12", "tool calls"], ["120s", "wall clock"]].forEach(([v, l], i) => {
    s.addText(v, { x: 7.12 + i * 1.78, y: 4.68, w: 1.7, h: 0.5, fontSize: 26, bold: true,
      color: C.accent1, margin: 0, isTextBox: true, objectName: `bound-${i}-v` });
    s.addText(l, { x: 7.12 + i * 1.78, y: 5.18, w: 1.7, h: 0.3, fontSize: 11.5,
      color: HEX.accent4, margin: 0, isTextBox: true, objectName: `bound-${i}-l` });
  });
  s.addText("A run that hits a bound escalates and names the bound. It never returns a confident answer.", {
    x: 7.12, y: 5.6, w: 5.26, h: 0.7, fontSize: 12.5, color: C.text1,
    lineSpacingMultiple: 1.12, margin: 0, isTextBox: true, objectName: "bounds-note",
  });
  s.addNotes("Five tools: corpus_search, find_named_clause, get_clause, clause_neighbours, get_claim_field. " +
    "find_named_clause exists specifically for the section 4.1 case.");
}

// =============================================================================
//  7 -- Knowing when not to answer
// =============================================================================
{
  const s = pres.addSlide({ masterName: "CONTENT_DARK", sectionTitle: "Design" });
  s.addText("Knowing when not to answer", { placeholder: "title" });

  card(s, { x: M, y: 1.3, w: 5.86, h: 2.44, fill: "2E3346", name: "verif-det" });
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: M + 0.3, y: 1.52, w: 2.1, h: 0.3,
    fill: { color: HEX.accent3 }, rectRadius: 0.14, objectName: "verif-det-tag" });
  s.addText("CHECK 1 · FREE", { x: M + 0.3, y: 1.52, w: 2.1, h: 0.3, align: "center",
    valign: "middle", fontSize: 9.5, bold: true, color: "FFFFFF", charSpacing: 1,
    margin: 0, isTextBox: true, objectName: "verif-det-taglabel" });
  s.addText("Does every quote actually exist?", {
    x: M + 0.3, y: 1.94, w: 5.26, h: 0.36, fontSize: 16, bold: true, color: "FFFFFF",
    margin: 0, isTextBox: true, objectName: "verif-det-head",
  });
  s.addText(
    "Each citation's quote is matched character-for-character against the clause it is attributed to. No model is needed to answer that, and none should be trusted with it. A citation that does not resolve is a fabrication whatever a verifier says about it.",
    { x: M + 0.3, y: 2.36, w: 5.26, h: 1.24, fontSize: 13, color: "C2C8D6",
      lineSpacingMultiple: 1.14, margin: 0, isTextBox: true, objectName: "verif-det-body" }
  );

  card(s, { x: 6.82, y: 1.3, w: 5.86, h: 2.44, fill: "2E3346", name: "verif-model" });
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 7.12, y: 1.52, w: 2.1, h: 0.3,
    fill: { color: HEX.accent2 }, rectRadius: 0.14, objectName: "verif-model-tag" });
  s.addText("CHECK 2 · MODEL", { x: 7.12, y: 1.52, w: 2.1, h: 0.3, align: "center",
    valign: "middle", fontSize: 9.5, bold: true, color: "232738", charSpacing: 1,
    margin: 0, isTextBox: true, objectName: "verif-model-taglabel" });
  s.addText("Is each claim actually entailed?", {
    x: 7.12, y: 1.94, w: 5.26, h: 0.36, fontSize: 16, bold: true, color: "FFFFFF",
    margin: 0, isTextBox: true, objectName: "verif-model-head",
  });
  s.addText(
    "Per-claim entailment against the passages that were available. “Consistent with” is not support. A statement the analyst could only have made by drawing on general insurance knowledge is unsupported, however correct it happens to be.",
    { x: 7.12, y: 2.36, w: 5.26, h: 1.24, fontSize: 13, color: "C2C8D6",
      lineSpacingMultiple: 1.14, margin: 0, isTextBox: true, objectName: "verif-model-body" }
  );

  card(s, { x: M, y: 3.94, w: CW, h: 1.16, fill: "3A2F2E", name: "invariant" });
  s.addText("Then, before the result is assembled: citations that did not resolve are dropped.", {
    x: M + 0.34, y: 4.08, w: CW - 0.68, h: 0.32, fontSize: 15, bold: true, color: "F3E3DF",
    margin: 0, isTextBox: true, objectName: "invariant-head",
  });
  s.addText("“Every citation must resolve to retrieved text” is therefore a property of the response object, not an instruction in a prompt — and it is tested as one.", {
    x: M + 0.34, y: 4.42, w: CW - 0.68, h: 0.56, fontSize: 13, color: "D8C9C5",
    margin: 0, isTextBox: true, objectName: "invariant-body",
  });

  s.addText("Eight reasons a run will decline to answer", {
    x: M, y: 5.28, w: CW, h: 0.34, fontSize: 14, bold: true, color: "E0A458",
    margin: 0, isTextBox: true, objectName: "esc-head",
  });
  const reasons = ["weak_retrieval", "conflicting_clauses", "outside_corpus", "verification_failed",
                   "loop_budget_exhausted", "provider_unavailable", "guardrail_block", "policy_requires_human"];
  reasons.forEach((r, i) => {
    const x = M + (i % 4) * 3.07;
    const y = 5.68 + Math.floor(i / 4) * 0.46;
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w: 2.86, h: 0.36,
      fill: { color: "2E3346" }, rectRadius: 0.06, objectName: `reason-${i}-box` });
    s.addText(r, { x: x + 0.16, y, w: 2.6, h: 0.36, fontSize: 11, color: "A7BEAE",
      valign: "middle", margin: 0, isTextBox: true, objectName: `reason-${i}` });
  });
  s.addNotes("This is the slide to linger on if the examiner asks what makes this different from a RAG demo.");
}

// =============================================================================
//  8 -- Architecture
// =============================================================================
pres.addSection({ title: "Platform" });
{
  const s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Platform" });
  s.addText("Ten services, and why each boundary is a boundary", { placeholder: "title" });

  const rows = [
    ["api  ↔  worker", "Scaling and failure isolation", "The API is microseconds per request and must stay up. The worker is seconds-to-a-minute and is the only process that talks to a model. A worker wedged on a provider timeout must not stop submissions.", HEX.accent1],
    ["worker  ↔  chroma", "Resource profile and release cadence", "Three replicas must share one index, not hold three copies. Re-indexing the corpus must not require a worker deploy.", HEX.accent2],
    ["llm-stub", "It stands in for a third party", "Reached through the same adapter as the real provider, so the load test measures the path we actually ship.", HEX.accent3],
    ["observability stack", "It outlives its subject", "prometheus · grafana · jaeger · cadvisor. If the worker is crash-looping, the dashboard that shows it crash-looping has to still be up.", HEX.accent4],
  ];
  rows.forEach(([name, why, body, col], i) => {
    const y = 1.34 + i * 1.26;
    card(s, { x: M, y, w: CW, h: 1.12, name: `bnd-${i}` });
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: M, y, w: 0.09, h: 1.12,
      fill: { color: col }, rectRadius: 0.04, objectName: `bnd-${i}-key` });
    s.addText(name, {
      x: M + 0.32, y: y + 0.14, w: 3.3, h: 0.4, fontSize: 13.5, bold: true, color: C.text2,
      margin: 0, isTextBox: true, objectName: `bnd-${i}-name`,
    });
    s.addText(why, {
      x: M + 0.32, y: y + 0.56, w: 3.3, h: 0.36, fontSize: 11, bold: true, color: col,
      margin: 0, isTextBox: true, objectName: `bnd-${i}-why`,
    });
    s.addText(body, {
      x: M + 3.82, y: y + 0.16, w: 8.1, h: 0.82, fontSize: 12.5, color: C.text1,
      lineSpacingMultiple: 1.12, margin: 0, isTextBox: true, objectName: `bnd-${i}-body`,
    });
  });

  s.addText(
    "Rejected: a separate retrieval service (an HTTP hop per call costs more than the separation recovers) · a separate validation service (a network hop before validation opens the very window the rule closes) · four services for the four agents.",
    { x: M, y: 6.4, w: CW, h: 0.44, fontSize: 11, italic: true, color: HEX.accent4,
      lineSpacingMultiple: 1.1, margin: 0, isTextBox: true, objectName: "rejected-note" }
  );
  s.addNotes("One image, two Dockerfile stages, four roles. The split that pays is the dependency one: the API image omits torch.");
}

// =============================================================================
//  9 -- Boundary and guardrails
// =============================================================================
{
  const s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Platform" });
  s.addText("Nothing reaches an agent unvalidated", { placeholder: "title" });

  const gates = [
    ["Rate limit", "per client", "429", HEX.accent4],
    ["Schema + type", "pydantic, contract fnol.v1", "422", HEX.accent2],
    ["Range + category", "enums, bounds, 38 state codes", "422", HEX.accent2],
    ["Cross-field", "14 named rules", "422", HEX.accent1],
    ["Guardrails", "injection + toxicity on free text", "422", HEX.accent1],
    ["Enqueue", "run id issued here, and only here", "202", HEX.accent3],
  ];
  gates.forEach(([name, sub, code, col], i) => {
    const x = M + i * 2.02;
    card(s, { x, y: 1.36, w: 1.86, h: 1.96, name: `gate-${i}` });
    s.addText(code, {
      x: x + 0.14, y: 1.52, w: 1.58, h: 0.42, fontSize: 19, bold: true, color: col,
      margin: 0, isTextBox: true, objectName: `gate-${i}-code`,
    });
    s.addText(name, {
      x: x + 0.14, y: 1.98, w: 1.58, h: 0.56, fontSize: 12.5, bold: true, color: C.text2,
      lineSpacingMultiple: 1.06, margin: 0, isTextBox: true, objectName: `gate-${i}-name`,
    });
    s.addText(sub, {
      x: x + 0.14, y: 2.54, w: 1.58, h: 0.66, fontSize: 10.5, color: HEX.accent4,
      lineSpacingMultiple: 1.08, margin: 0, isTextBox: true, objectName: `gate-${i}-sub`,
    });
  });

  card(s, { x: M, y: 3.5, w: 5.86, h: 1.68, fill: "EBF1EF", name: "nostart" });
  s.addText("A rejection cannot start a run", {
    x: M + 0.3, y: 3.68, w: 5.26, h: 0.34, fontSize: 15, bold: true, color: C.accent3,
    margin: 0, isTextBox: true, objectName: "nostart-head",
  });
  s.addText(
    "The quarantine record's agent_run_started field is typed Literal[False]. It is not a field that happens to be false — it is one that cannot be true, because no code path leads from the rejection branch to a run.",
    { x: M + 0.3, y: 4.04, w: 5.26, h: 1.02, fontSize: 12.5, color: C.text1,
      lineSpacingMultiple: 1.12, margin: 0, isTextBox: true, objectName: "nostart-body" }
  );

  card(s, { x: 6.82, y: 3.5, w: 5.86, h: 1.68, fill: "F7EDEB", name: "surfaces" });
  s.addText("Three guardrail surfaces, not one", {
    x: 7.12, y: 3.68, w: 5.26, h: 0.34, fontSize: 15, bold: true, color: C.accent6,
    margin: 0, isTextBox: true, objectName: "surfaces-head",
  });
  s.addText(
    "input (the customer's text) · tool (corpus text on its way into a prompt) · output (the determination). The tool surface is the one people forget: an agent holds tools, and text that reaches it can try to use them.",
    { x: 7.12, y: 4.04, w: 5.26, h: 1.02, fontSize: 12.5, color: C.text1,
      lineSpacingMultiple: 1.12, margin: 0, isTextBox: true, objectName: "surfaces-body" }
  );

  card(s, { x: M, y: 5.32, w: CW, h: 1.42, fill: C.text2, name: "output-guard" });
  s.addText("The output filter is what makes the system safe regardless of how an answer got drafted", {
    x: M + 0.34, y: 5.5, w: CW - 0.68, h: 0.34, fontSize: 15, bold: true, color: "E0A458",
    margin: 0, isTextBox: true, objectName: "og-head",
  });
  s.addText(
    "If the model writes “the claim is approved and we will pay the full amount”, that sentence never leaves the process. The run is converted to a human escalation, the coverage position is replaced with undetermined, and the guardrail event is attached to the result. The service is advisory by construction, not by instruction.",
    { x: M + 0.34, y: 5.88, w: CW - 0.68, h: 0.76, fontSize: 13, color: "C2C8D6",
      lineSpacingMultiple: 1.12, margin: 0, isTextBox: true, objectName: "og-body" }
  );
  s.addNotes("Guardrails are rules, not a classifier — deliberate: a model call per field on a free tier, and an unauditable block. Say so before being asked.");
}

// =============================================================================
//  10 -- Evaluation
// =============================================================================
pres.addSection({ title: "Results" });
{
  const s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Results" });
  s.addText("How it is evaluated", { placeholder: "title" });

  [
    ["25", "scenarios, each with the\npassage that supports it", C.accent1],
    ["167", "tests, 4 seconds,\nno infrastructure needed", C.accent3],
    ["4", "metrics, each with its\ncomputation written down", C.accent2],
    ["6", "must be rejected before\nany agent run starts", HEX.accent4],
  ].forEach(([v, l, col], i) => {
    stat(s, { x: M + i * 3.07, y: 1.36, w: 2.8, value: v, label: l, color: col, name: `evalstat-${i}` });
  });

  const metrics = [
    ["groundedness", "Share of the determination's statements that some retrieved passage supports. Content-word recall ≥ 0.45, a threshold set by hand-labelling 40 statements."],
    ["context relevance", "Share of the passages shown to the model that bear on the claim. Threshold deliberately low — a clause and a claim share little vocabulary by nature."],
    ["faithfulness", "Share of citations whose quote is found character-for-character in the clause it names. The strictest of the four, and the one that cannot be argued with."],
    ["hallucination rate", "Share of records asserting a confident position they could not support. Escalating is explicitly NOT counted — a metric that punished declining to answer would push the system the wrong way."],
  ];
  metrics.forEach(([name, body], i) => {
    const y = 2.92 + i * 0.95;
    card(s, { x: M, y, w: CW, h: 0.85, name: `metric-${i}` });
    s.addText(name, {
      x: M + 0.3, y: y + 0.1, w: 2.5, h: 0.65, fontSize: 13.5, bold: true, color: C.accent1,
      valign: "middle", margin: 0, isTextBox: true, objectName: `metric-${i}-name`,
    });
    s.addText(body, {
      x: M + 2.95, y: y + 0.1, w: 8.9, h: 0.65, fontSize: 11.5, color: C.text1,
      valign: "middle", lineSpacingMultiple: 1.1, margin: 0, isTextBox: true,
      objectName: `metric-${i}-body`,
    });
  });

  s.addText("Covering the reference case and its control, restricted use, the driver's clause, conditions, out-of-corpus escalation, guardrails and contract validation.", {
    x: M, y: 6.76, w: CW, h: 0.3, fontSize: 10.5, italic: true, color: HEX.accent4,
    margin: 0, isTextBox: true, objectName: "eval-foot",
  });
  s.addNotes("The control scenario matters: commuting to work is also a 'purpose'. A system that fails it has learned the wrong rule.");
}

// =============================================================================
//  11 -- Performance
// =============================================================================
{
  const s = pres.addSlide({ masterName: "CHART_SLIDE", sectionTitle: "Results" });
  s.addText("The objective, declared before measuring", { placeholder: "title" });

  s.addChart(
    pres.charts.BAR,
    [{
      name: "p95 of a complete run (s)",
      labels: ["5", "20", "50", "150", "400", "600", "800"],
      values: [2.6, 2.6, 2.7, 7.9, 22.0, 41.0, 57.0],
    }],
    {
      placeholder: "chart",
      barDir: "col",
      chartColors: [HEX.accent3, HEX.accent3, HEX.accent3, HEX.accent3, HEX.accent2, HEX.accent2, HEX.accent1],
      ...CHART_BASE,
      dataLabelPosition: "outEnd",
      dataLabelFormatCode: '0.0"s"',
      catAxisTitle: "concurrent callers",
      showCatAxisTitle: true,
      catAxisTitleColor: HEX.accent4,
      catAxisTitleFontSize: 11,
      catAxisTitleFontFace: "+mn-lt",
      valAxisMaxVal: 65,
      barGapWidthPct: 45,
    }
  );

  s.addText(
    [
      { text: "p95 ≤ 45 s, submission to final result\n", options: { fontSize: 15, bold: true, color: HEX.accent1, breakLine: true } },
      { text: "Written into .env.example, the Prometheus alert and the dashboard threshold before the first load test ran. Not a customer-facing wait: the submission call returns in 4 ms.\n\n", options: { fontSize: 12.5, color: HEX.dk1, breakLine: true } },
      { text: "The target fails at about 640 callers.\n", options: { fontSize: 13.5, bold: true, color: HEX.accent6, breakLine: true } },
      { text: "Throughput peaks near 150 callers, then falls as latency climbs.\n\n", options: { fontSize: 12.5, color: HEX.dk1, breakLine: true } },
      { text: "The component responsible: a 40-thread pool.\n", options: { fontSize: 13.5, bold: true, color: HEX.accent6, breakLine: true } },
      { text: "triage_inflight_runs pinned at 38, never above 40. Little's law: 40 slots ÷ 2.6 s = 15.4 runs/s — the measured ceiling.", options: { fontSize: 12.5, color: HEX.dk1 } },
    ],
    { placeholder: "body", lineSpacingMultiple: 1.1 }
  );

  s.addText("Measured outside Docker in inline execution mode — so the ceiling and the bottleneck are artefacts of that topology, not the queue-and-worker one. Stated as such in docs/load-test.md.", {
    x: M, y: 6.44, w: CW, h: 0.42, fontSize: 10.5, italic: true, color: HEX.accent4,
    margin: 0, isTextBox: true, objectName: "perf-caveat",
  });
  s.addNotes("If asked why the numbers are not from containers: Docker was not installed on the build machine. Say it plainly; the limitation is documented.");
}

// =============================================================================
//  12 -- Cost
// =============================================================================
{
  const s = pres.addSlide({ masterName: "CHART_SLIDE", sectionTitle: "Results" });
  s.addText("Where the tokens go, and what caching is worth", { placeholder: "title" });

  s.addChart(
    pres.charts.BAR,
    [{
      name: "tokens per run",
      labels: ["coverage_analysis", "verification", "query_formulation", "routing", "intake_classification"],
      values: [4976, 4484, 4140, 936, 854],
    }],
    {
      placeholder: "chart",
      barDir: "bar",
      chartColors: [HEX.accent1, HEX.accent1, HEX.accent1, HEX.accent4, HEX.accent4],
      ...CHART_BASE,
      dataLabelPosition: "outEnd",
      valAxisMaxVal: 6000,
      barGapWidthPct: 40,
      catAxisLabelFontSize: 11.5,
    }
  );

  s.addText(
    [
      { text: "15,390 tokens per run\n", options: { fontSize: 17, bold: true, color: HEX.accent1, breakLine: true } },
      { text: "1.54 M per 100 runs, 96% of it input. USD 0.00 on a free tier; about $0.94 per 100 runs on a mid-size hosted model.\n\n", options: { fontSize: 12.5, color: HEX.dk1, breakLine: true } },
      { text: "The driver is the retrieved context block.\n", options: { fontSize: 13.5, bold: true, color: HEX.accent6, breakLine: true } },
      { text: "The three expensive steps are exactly the three that receive it — 88% of the run — and it is serialised into the prompt three times. Trimming the verifier's context to the cited clauses would save roughly 25%.\n\n", options: { fontSize: 12.5, color: HEX.dk1, breakLine: true } },
      { text: "It was not done, and that is recorded.", options: { fontSize: 12.5, italic: true, color: HEX.accent4 } },
    ],
    { placeholder: "body", lineSpacingMultiple: 1.1 }
  );

  card(s, { x: M, y: 6.3, w: CW, h: 0.6, fill: C.background2, name: "cache-strip" });
  s.addText(
    [
      { text: "Run cache, repeated claim:  ", options: { fontSize: 12.5, color: HEX.dk1 } },
      { text: "p95 2.368 s → 0.109 s   ·   tokens 14,771 → 739   ·   −95% on both", options: { fontSize: 12.5, bold: true, color: HEX.accent3 } },
      { text: "      Unique claims:  ", options: { fontSize: 12.5, color: HEX.dk1 } },
      { text: "no latency effect, ~5% of tokens", options: { fontSize: 12.5, bold: true, color: HEX.accent4 } },
    ],
    { x: M + 0.34, y: 6.3, w: CW - 0.68, h: 0.6, valign: "middle", margin: 0,
      isTextBox: true, objectName: "cache-strip-text" }
  );
  s.addNotes("The honest half: the embedding and retrieval caches buy almost nothing, because the embedder and index are both local. We measured it rather than asserting 'caching helps'.");
}

// =============================================================================
//  13 -- What went wrong
// =============================================================================
pres.addSection({ title: "Judgement" });
{
  const s = pres.addSlide({ masterName: "CONTENT_DARK", sectionTitle: "Judgement" });
  s.addText("Eight incidents — six of them reported success", { placeholder: "title" });

  const incidents = [
    ["Every clause filed under the wrong chapter", "A trailing space in the PDF defeated the line-joining test, so a chapter heading was swallowed into the paragraph above. Citations would have pointed at the wrong page. Nothing failed."],
    ["The guardrail rewrote the text it protected", "It neutralised directives inside corpus text the model then quoted. Quotes no longer matched the stored clause, so runs looped and escalated, looking like a retrieval fault."],
    ["The load test declared the objective met on 100% failures", "Hard-coded policy dates against a now()-derived loss date. Every claim was rejected in 3 ms, so the run looked fast: p95 0.00 s, target met."],
    ["The cache made its own benefit invisible", "A cache hit replayed the originating run's token count, inflating cost-per-100-runs by exactly the amount the cache saved."],
    ["The largest cost driver was reported as the cheapest step", "max() over per-step spend, where every price is zero on a free tier, returns whichever key comes first."],
    ["The same date bug again, in a second file", "The web client paired a now()-derived loss date with a hard-coded policy period. Unusable for months while every test passed against a different fixture."],
  ];
  incidents.forEach(([head, body], i) => {
    const y = 1.26 + i * 0.71;
    card(s, { x: M, y, w: CW, h: 0.64, fill: "2E3346", name: `inc-${i}` });
    s.addText(`0${i + 1}`, {
      x: M + 0.26, y: y + 0.06, w: 0.6, h: 0.52, fontSize: 16, bold: true, color: HEX.accent2,
      valign: "middle", margin: 0, isTextBox: true, objectName: `inc-${i}-n`,
    });
    s.addText(head, {
      x: M + 0.92, y: y + 0.07, w: 4.5, h: 0.5, fontSize: 12, bold: true, color: "FFFFFF",
      valign: "middle", lineSpacingMultiple: 1.04, margin: 0, isTextBox: true,
      objectName: `inc-${i}-head`,
    });
    s.addText(body, {
      x: M + 5.56, y: y + 0.05, w: 6.3, h: 0.56, fontSize: 10, color: "B9C0CE",
      valign: "middle", lineSpacingMultiple: 1.08, margin: 0, isTextBox: true,
      objectName: `inc-${i}-body`,
    });
  });

  card(s, { x: M, y: 6.24, w: CW, h: 0.62, fill: "3A2F2E", name: "thread" });
  s.addText(
    [
      { text: "The common thread:  ", options: { bold: true, color: "E0A458" } },
      { text: "each produced a plausible-looking result rather than an error — the argument for the deterministic citation check, the valid_for_reporting flag, and a load test that refuses to conclude.", options: { color: "F3E3DF" } },
    ],
    { x: M + 0.34, y: 6.24, w: CW - 0.68, h: 0.62, fontSize: 11.5, valign: "middle",
      lineSpacingMultiple: 1.08, margin: 0, isTextBox: true, objectName: "thread-text" }
  );
  s.addNotes("This is the engineering-judgement slide. Do not rush it — it is worth more than any feature slide.");
}

// =============================================================================
//  14 -- What is not finished
// =============================================================================
{
  const s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Judgement" });
  s.addText("What is not finished", { placeholder: "title" });

  const gaps = [
    ["docker compose up has never been run", "Docker is not installed on the build machine. Every service, stage and health check is written and reviewed; the whole Python system is verified outside containers. The container build is the first thing to check.", HEX.accent1],
    ["--scale worker=3 not measured", "docs/scaling.md gives the capacity model, the method and a decision procedure for each candidate bottleneck — rather than a number that was not taken.", HEX.accent2],
    ["Every evaluation number is stub-derived", "Flagged valid_for_reporting: false in each report. The harness works; answer quality is unmeasured.", HEX.accent2],
    ["The prompt comparison returns a zero delta", "The stub reads only the step tag, so it cannot distinguish v1 from v2. prompt-compare now refuses to run against it rather than print zeros.", HEX.accent1],
  ];
  gaps.forEach(([head, body, col], i) => {
    const y = 1.34 + i * 1.22;
    card(s, { x: M, y, w: 7.7, h: 1.1, name: `gap-${i}` });
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: M, y, w: 0.09, h: 1.1,
      fill: { color: col }, rectRadius: 0.04, objectName: `gap-${i}-key` });
    s.addText(head, {
      x: M + 0.32, y: y + 0.12, w: 7.1, h: 0.32, fontSize: 13, bold: true, color: C.text2,
      margin: 0, isTextBox: true, objectName: `gap-${i}-head`,
    });
    s.addText(body, {
      x: M + 0.32, y: y + 0.44, w: 7.1, h: 0.6, fontSize: 11, color: C.text1,
      lineSpacingMultiple: 1.08, margin: 0, isTextBox: true, objectName: `gap-${i}-body`,
    });
  });

  card(s, { x: 8.6, y: 1.34, w: 4.08, h: 4.76, fill: C.text2, name: "next" });
  s.addText("If there were another week", {
    x: 8.9, y: 1.56, w: 3.5, h: 0.34, fontSize: 15, bold: true, color: "E0A458",
    margin: 0, isTextBox: true, objectName: "next-head",
  });
  [
    "Run docker compose up, then --scale worker=3, and replace the predictions with measurements.",
    "Point it at a real provider and run prompt-compare. If change C1 does not move the restricted-use scenarios, the central hypothesis is wrong.",
    "Trim the verifier's context to the cited clauses. 25% of tokens, small change.",
    "Move the queue to Redis Streams with consumer groups for at-least-once delivery.",
  ].forEach((t, i) => {
    const y = 2.02 + i * 0.96;
    s.addShape(pres.shapes.OVAL, { x: 8.9, y, w: 0.3, h: 0.3,
      fill: { color: HEX.accent1 }, objectName: `next-${i}-dot` });
    s.addText(String(i + 1), { x: 8.9, y, w: 0.3, h: 0.3, align: "center", valign: "middle",
      fontSize: 12, bold: true, color: "FFFFFF", margin: 0, isTextBox: true,
      objectName: `next-${i}-n` });
    s.addText(t, { x: 9.34, y: y - 0.04, w: 3.04, h: 0.86, fontSize: 10.5, color: "C2C8D6",
      lineSpacingMultiple: 1.1, margin: 0, isTextBox: true, objectName: `next-${i}-t` });
  });

  s.addText("The brief says completing all 22 requirements is not expected and that sound prioritisation is itself assessed. This is the list, rather than a gap to be found.", {
    x: M, y: 6.22, w: 7.7, h: 0.5, fontSize: 11.5, italic: true, color: HEX.accent4,
    lineSpacingMultiple: 1.1, margin: 0, isTextBox: true, objectName: "gaps-foot",
  });
  s.addNotes("Volunteer this slide. An examiner who finds an undisclosed gap trusts nothing else; one who is told the gaps trusts the rest.");
}

// =============================================================================
//  15 -- Close / demo
// =============================================================================
pres.addSection({ title: "Close" });
{
  const s = pres.addSlide({ masterName: "SECTION_DARK", sectionTitle: "Close" });
  s.addText("DEMONSTRATION", { placeholder: "eyebrow" });
  s.addText("Let's run the case", { placeholder: "title" });
  s.addText("scripts/demo.sh — the section 4.1 reference claim, end to end", { placeholder: "body" });

  const items = [
    ["Valid claim → routed recommendation", "citations that resolve"],
    ["One run traced end to end", "every tool call, query, prompt version, cost"],
    ["Malformed request", "quarantined, no agent run started"],
    ["Prompt injection", "blocked at the boundary"],
    ["Provider outage", "circuit opens, degradation path visible"],
    ["The section 4.1 case", "not covered, investigation, both clauses cited"],
  ];
  items.forEach(([head, sub], i) => {
    const x = M + (i % 3) * 4.07;
    const y = 5.42 + Math.floor(i / 3) * 0.78;
    s.addShape(pres.shapes.OVAL, { x, y: y + 0.08, w: 0.26, h: 0.26,
      fill: { color: HEX.accent3 }, objectName: `demo-${i}-dot` });
    s.addText(head, { x: x + 0.4, y, w: 3.5, h: 0.3, fontSize: 12, bold: true,
      color: "FFFFFF", margin: 0, isTextBox: true, objectName: `demo-${i}-head` });
    s.addText(sub, { x: x + 0.4, y: y + 0.28, w: 3.5, h: 0.3, fontSize: 10.5,
      color: "8FA3B8", margin: 0, isTextBox: true, objectName: `demo-${i}-sub` });
  });
  s.addNotes("Run scripts/smoke.sh before the session so nothing is discovered live. It prints PASS or FAIL for all ten demonstration items.");
}

// -----------------------------------------------------------------------------
(async () => {
  const out = path.join(__dirname, "claim-triage-deck.pptx");
  await pres.writeFile({ fileName: out });
  const { applyTheme } = require(process.env.PPTX_SKILL_DIR
    ? path.join(process.env.PPTX_SKILL_DIR, "scripts", "apply_theme.js")
    : "./apply_theme.js");
  await applyTheme(out, THEME);
  console.log("wrote", out);
})();
