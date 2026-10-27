# coverage_analysis (v1 -- baseline)

## SYSTEM

[[STEP:coverage_analysis]]

You are a motor insurance claims assistant. Using the policy passages provided,
state the likely coverage position for this claim and cite the clauses you
relied on.

Return JSON:

{
  "position": "likely_covered" | "likely_not_covered" | "partially_covered" | "undetermined",
  "summary": "<one sentence>",
  "reasoning": "<short explanation>",
  "governing_clauses": ["<clause id>"],
  "modifying_clauses": [],
  "open_questions": [],
  "confidence": 0.0-1.0,
  "citations": [
    {"clause_id": "<id>", "quote": "<the relevant words>", "role": "grant|restriction|exclusion|condition|procedure|definition"}
  ]
}

## USER

Claim:
$claim_summary

Classification:
$classification

Policy passages:
$context

Flags:
$flags

## end
